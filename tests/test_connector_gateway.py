"""Contract tests for local Connector enrollment and typed control."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from connector_gateway.models import CommandAction, RuntimeInventoryItem
from connector_gateway.service import ConnectorError, ConnectorGateway
from web.api import create_app


def _enroll(client: TestClient):
    pairing_response = client.post(
        "/api/connectors/pairings",
        json={"owner_id": "owner-1", "device_name": "Alice laptop"},
    )
    assert pairing_response.status_code == 201
    pairing = pairing_response.json()

    waiting_response = client.post(
        "/api/connectors/pairings/exchange",
        json={"device_code": pairing["device_code"]},
    )
    assert waiting_response.status_code == 428

    approval_response = client.post(
        f"/api/connectors/pairings/{pairing['user_code']}/approve",
        json={"owner_id": "owner-1"},
    )
    assert approval_response.status_code == 200
    assert approval_response.json()["status"] == "approved"

    exchange_response = client.post(
        "/api/connectors/pairings/exchange",
        json={"device_code": pairing["device_code"]},
    )
    assert exchange_response.status_code == 200
    credential = exchange_response.json()
    assert credential["device_token"]
    return pairing, credential


def _arena_decide_task() -> dict:
    return {
        "taskId": "task-arena-decide-1",
        "kind": "arena.decide",
        "schemaVersion": "arena.agent-task.v1",
        "gameId": "game-1",
        "roundId": "round-1",
        "gameAgentId": "game-agent-1",
        "negotiationId": None,
        "deadlineAt": "2030-07-25T12:00:30Z",
        "idempotencyKey": "game-1:round-1:game-agent-1:decide",
        "inputHash": "sha256:" + ("0" * 64),
        "input": {
            "phase": "decide",
            "gameId": "game-1",
            "roundId": "round-1",
            "roundIndex": 1,
            "cash": "20.000000",
            "holdings": {"grain": 1},
            "market": {"grain": "2.000000"},
            "events": [],
            "reputation": {"failedNegotiations": 0},
            "limits": {
                "allowedActions": ["buy", "sell", "pass"],
                "allowedGoods": ["grain"],
            },
            "completedActions": [],
            "completedTrades": [],
            "goods": [
                {
                    "good": "grain",
                    "fixedQuantity": 1,
                    "priceDecimalPlaces": 6,
                }
            ],
            "deadlineAt": "2030-07-25T12:00:30Z",
        },
    }


def test_unauthenticated_connector_control_plane_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ADX_CONNECTOR_UNSAFE_DEMO", raising=False)
    client = TestClient(create_app())

    assert client.app.state.connector_gateway_enabled is False
    response = client.post(
        "/api/connectors/pairings",
        json={"device_name": "must not be remotely exposed"},
    )
    assert response.status_code == 404

    remote_client = TestClient(
        create_app(connector_demo_enabled=True),
        client=("203.0.113.7", 32100),
    )
    remote_response = remote_client.post(
        "/api/connectors/pairings",
        json={"device_name": "remote client must be rejected"},
        headers={"X-Forwarded-For": "127.0.0.1"},
    )
    assert remote_response.status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with remote_client.websocket_connect("/api/connectors/ws?device_id=untrusted"):
            pass


def test_pairing_is_one_time_and_secrets_are_not_exposed():
    client = TestClient(create_app(connector_demo_enabled=True))
    pairing, credential = _enroll(client)

    reused = client.post(
        "/api/connectors/pairings/exchange",
        json={"device_code": pairing["device_code"]},
    )
    assert reused.status_code == 401

    devices = client.get(
        "/api/connectors/devices", params={"owner_id": "owner-1"}
    ).json()["devices"]
    assert len(devices) == 1
    assert devices[0]["device_id"] == credential["device_id"]
    assert "device_token" not in devices[0]
    assert "token_hash" not in devices[0]
    assert devices[0]["status"] == "offline"

    wrong_owner = client.post(
        f"/api/connectors/devices/{credential['device_id']}/revoke",
        json={"owner_id": "owner-2"},
    )
    assert wrong_owner.status_code == 403
    revoked = client.post(
        f"/api/connectors/devices/{credential['device_id']}/revoke",
        json={"owner_id": "owner-1"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    with client.websocket_connect(
        f"/api/connectors/ws?device_id={credential['device_id']}",
        headers={"Authorization": f"Device {credential['device_token']}"},
    ) as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == 4403

    bind_after_revoke = client.post(
        f"/api/connectors/devices/{credential['device_id']}/bindings",
        json={"runtime_id": "codex-default"},
    )
    assert bind_after_revoke.status_code == 410


def test_connection_setup_failure_is_not_reported_as_replacement():
    client = TestClient(create_app(connector_demo_enabled=True))
    _, credential = _enroll(client)
    service = client.app.state.connector_gateway

    async def fail_connection_setup(device_id, websocket):
        raise ConnectorError(500, "synthetic setup failure")

    service.connect_device = fail_connection_setup
    socket_path = f"/api/connectors/ws?device_id={credential['device_id']}"
    with client.websocket_connect(
        socket_path,
        headers={"Authorization": f"Device {credential['device_token']}"},
    ) as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()

    assert closed.value.code == 1011
    assert closed.value.reason == "Connector connection setup failed"


def test_hello_ack_includes_only_device_binding_refs_for_mcp_sync():
    client = TestClient(create_app(connector_demo_enabled=True))
    _, credential = _enroll(client)
    device_id = credential["device_id"]
    service = client.app.state.connector_gateway

    async def prepare_binding():
        await service.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex CLI",
                    executable_path="C:\\tools\\codex.exe",
                    available=True,
                    task_enabled=True,
                    authentication_status="configured",
                    arena_compatible=True,
                    arena_isolation="read_only_ephemeral_schema",
                    local_execution_ready=True,
                )
            ],
        )
        return await service.create_binding(
            device_id,
            "codex-default",
            None,
            "MCP binding",
        )

    binding = asyncio.run(prepare_binding())
    socket_path = f"/api/connectors/ws?device_id={device_id}"
    with client.websocket_connect(
        socket_path,
        headers={"Authorization": f"Device {credential['device_token']}"},
    ) as socket:
        assert socket.receive_json()["type"] == "welcome"
        socket.send_json(
            {
                "type": "hello",
                "message_id": "hello-mcp-sync",
                "payload": {
                    "protocol_version": "1.0",
                    "connector_version": "0.1.0",
                    "platform": "windows/amd64",
                    "hostname": "alice-pc",
                },
            }
        )
        acknowledgement = socket.receive_json()

    assert acknowledgement["type"] == "ack"
    assert acknowledgement["payload"]["mcp_bindings"] == [
        {
            "binding_id": binding["binding_id"],
            "binding_epoch": binding["binding_epoch"],
        }
    ]
    assert set(acknowledgement["payload"]["mcp_bindings"][0]) == {
        "binding_id",
        "binding_epoch",
    }


def test_outbound_socket_inventory_binding_command_and_event_flow():
    client = TestClient(create_app(connector_demo_enabled=True))
    _, credential = _enroll(client)
    device_id = credential["device_id"]

    socket_path = f"/api/connectors/ws?device_id={device_id}"
    with client.websocket_connect(
        socket_path,
        headers={"Authorization": f"Device {credential['device_token']}"},
    ) as socket:
        welcome = socket.receive_json()
        assert welcome["type"] == "welcome"
        assert welcome["payload"]["protocol_version"] == "1.0"

        socket.send_json(
            {
                "type": "hello",
                "message_id": "hello-1",
                "payload": {
                    "protocol_version": "1.0",
                    "connector_version": "0.1.0",
                    "platform": "windows/amd64",
                    "hostname": "alice-pc",
                },
            }
        )
        hello_ack = socket.receive_json()
        assert hello_ack["type"] == "ack"
        assert hello_ack["message_id"] == "hello-1"

        socket.send_json(
            {
                "type": "inventory.snapshot",
                "message_id": "inventory-1",
                "payload": {
                    "runtimes": [
                        {
                            "runtime_id": "codex-default",
                            "kind": "codex",
                            "display_name": "Codex CLI",
                            "executable_path": "C:\\tools\\codex.exe",
                            "version": "codex-cli 1.2.3",
                            "available": True,
                            "capabilities": [
                                "session.start",
                                "task.dispatch",
                                "task.cancel",
                            ],
                            "auth_modes": ["chatgpt"],
                            "task_enabled": True,
                            "authentication_status": "configured",
                            "arena_compatible": True,
                            "arena_isolation": "read_only_ephemeral_schema",
                            "local_execution_ready": True,
                            "readiness_issues": [],
                        }
                    ]
                },
            }
        )
        inventory_ack = socket.receive_json()
        assert inventory_ack["type"] == "ack"
        assert inventory_ack["payload"]["runtime_count"] == 1

        device = client.get(f"/api/connectors/devices/{device_id}").json()
        assert device["status"] == "online"
        assert device["runtimes"][0]["kind"] == "codex"
        assert device["runtimes"][0]["local_execution_ready"] is True
        assert device["runtimes"][0]["arena_compatible"] is True
        assert device["runtimes"][0]["arena_isolation"] == "read_only_ephemeral_schema"

        binding_response = client.post(
            f"/api/connectors/devices/{device_id}/bindings",
            json={
                "runtime_id": "codex-default",
                "agent_id": "agent-alice",
                "display_name": "Alice Codex",
                "working_directory": "E:\\workspace",
            },
        )
        assert binding_response.status_code == 201
        binding = binding_response.json()
        assert binding["working_directory"] == "E:\\workspace"

        command_response = client.post(
            f"/api/connectors/bindings/{binding['binding_id']}/commands",
            json={
                "action": "session.start",
                "payload": {
                    "working_directory": "E:\\workspace",
                    "initial_prompt": "Enter the ADX arena.",
                },
                "idempotency_key": "start-session-1",
            },
        )
        assert command_response.status_code == 202
        command = command_response.json()

        delivered = socket.receive_json()
        assert delivered["type"] == "command"
        assert delivered["payload"]["action"] == "session.start"
        assert "shell" not in delivered["payload"]["payload"]

        socket.send_json(
            {
                "type": "command.ack",
                "message_id": "command-ack-1",
                "payload": {
                    "command_id": command["command_id"],
                    "status": "running",
                    "session_id": "session-1",
                },
            }
        )
        assert socket.receive_json()["type"] == "ack"

        socket.send_json(
            {
                "type": "runtime.event",
                "message_id": "event-1",
                "payload": {
                    "binding_id": binding["binding_id"],
                    "session_id": "session-1",
                    "sequence": 1,
                    "event_type": "assistant.output.delta",
                    "data": {"text": "Ready"},
                },
            }
        )
        event_ack = socket.receive_json()
        assert event_ack["type"] == "event.ack"
        assert event_ack["payload"]["through_sequence"] == 1

        # A cumulative ACK must not skip a missing durable-outbox event.
        for sequence, expected_watermark in ((3, 1), (2, 3)):
            socket.send_json(
                {
                    "type": "runtime.event",
                    "message_id": f"event-{sequence}",
                    "payload": {
                        "event_id": f"connector-event-{sequence}",
                        "binding_id": binding["binding_id"],
                        "session_id": "session-1",
                        "sequence": sequence,
                        "event_type": "assistant.output.delta",
                        "data": (
                            {
                                "text": "sk-abcdefghijklmnopqrstuvwxyz",
                                "nested": {"api_key": "do-not-store-me"},
                            }
                            if sequence == 3
                            else {"text": str(sequence)}
                        ),
                    },
                }
            )
            gap_ack = socket.receive_json()
            assert gap_ack["type"] == "event.ack"
            assert gap_ack["payload"]["through_sequence"] == expected_watermark

        socket.send_json(
            {
                "type": "command.ack",
                "message_id": "command-final-1",
                "payload": {
                    "command_id": command["command_id"],
                    "status": "succeeded",
                    "result": {"session_id": "session-1"},
                },
            }
        )
        assert socket.receive_json()["type"] == "ack"
        commands = client.get(
            f"/api/connectors/bindings/{binding['binding_id']}/commands"
        ).json()["commands"]
        assert commands[0]["status"] == "succeeded"
        assert "request_fingerprint" not in commands[0]

        events = client.get(
            f"/api/connectors/bindings/{binding['binding_id']}/events"
        ).json()["events"]
        assert events[0]["event_type"] == "assistant.output.delta"
        assert events[0]["data"]["text"] == "Ready"
        redacted_event = next(event for event in events if event["sequence"] == 3)
        assert redacted_event["data"]["text"] == "[REDACTED]"
        assert redacted_event["data"]["nested"]["api_key"] == "[REDACTED]"

        repeated = client.post(
            f"/api/connectors/bindings/{binding['binding_id']}/commands",
            json={
                "action": "session.start",
                "payload": {
                    "working_directory": "E:\\workspace",
                    "initial_prompt": "Enter the ADX arena.",
                },
                "idempotency_key": "start-session-1",
            },
        )
        assert repeated.json()["command_id"] == command["command_id"]

        conflicting_reuse = client.post(
            f"/api/connectors/bindings/{binding['binding_id']}/commands",
            json={
                "action": "session.start",
                "payload": {
                    "working_directory": "E:\\different-workspace",
                },
                "idempotency_key": "start-session-1",
            },
        )
        assert conflicting_reuse.status_code == 409

    device = client.get(f"/api/connectors/devices/{device_id}").json()
    assert device["status"] == "offline"


def test_command_surface_rejects_arbitrary_execution_and_missing_fields():
    client = TestClient(create_app(connector_demo_enabled=True))
    _, credential = _enroll(client)
    service = client.app.state.connector_gateway
    device = service.devices[credential["device_id"]]
    device["runtimes"] = [
        {
            "runtime_id": "codex-default",
            "kind": "codex",
            "display_name": "Codex CLI",
            "executable_path": "codex",
            "available": True,
            "capabilities": [],
            "auth_modes": [],
        }
    ]
    binding = client.post(
        f"/api/connectors/devices/{credential['device_id']}/bindings",
        json={"runtime_id": "codex-default"},
    ).json()

    arbitrary = client.post(
        f"/api/connectors/bindings/{binding['binding_id']}/commands",
        json={
            "action": "session.start",
            "payload": {"shell": "rm -rf /"},
        },
    )
    assert arbitrary.status_code == 422
    assert "forbidden" in arbitrary.json()["detail"].lower()

    for payload in ({}, {"working_directory": ""}, {"working_directory": "   "}):
        invalid_workspace = client.post(
            f"/api/connectors/bindings/{binding['binding_id']}/commands",
            json={"action": "session.start", "payload": payload},
        )
        assert invalid_workspace.status_code == 422
        assert "working_directory" in invalid_workspace.json()["detail"]

    detection_only = client.post(
        f"/api/connectors/bindings/{binding['binding_id']}/commands",
        json={
            "action": "session.start",
            "payload": {"working_directory": "E:\\workspace"},
        },
    )
    assert detection_only.status_code == 409
    assert "does not advertise session.start" in detection_only.json()["detail"]

    missing = client.post(
        f"/api/connectors/bindings/{binding['binding_id']}/commands",
        json={"action": "task.dispatch", "payload": {"prompt": "hello"}},
    )
    assert missing.status_code == 422
    assert "session_id" in missing.json()["detail"]

    resume_without_managed_session = client.post(
        f"/api/connectors/bindings/{binding['binding_id']}/commands",
        json={
            "action": "session.resume",
            "payload": {"session_id": "not-a-managed-session"},
        },
    )
    assert resume_without_managed_session.status_code == 409
    assert "Connector-owned session" in resume_without_managed_session.json()["detail"]

    for action, payload in (
        (
            "session.start",
            {
                "working_directory": "E:\\workspace",
                "conversation_id": "provider-conversation",
            },
        ),
        (
            "session.resume",
            {
                "session_id": "not-a-managed-session",
                "conversation_id": "provider-conversation",
            },
        ),
        (
            "session.resume",
            {
                "session_id": "not-a-managed-session",
                "resume_token": "provider-token",
            },
        ),
    ):
        cloud_resume_token = client.post(
            f"/api/connectors/bindings/{binding['binding_id']}/commands",
            json={"action": action, "payload": payload},
        )
        assert cloud_resume_token.status_code == 422
        assert "forbidden" in cloud_resume_token.json()["detail"].lower()

    unknown_action = client.post(
        f"/api/connectors/bindings/{binding['binding_id']}/commands",
        json={"action": "shell.exec", "payload": {}},
    )
    assert unknown_action.status_code == 422


class _FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed: list[tuple[int, str]] = []

    async def send_json(self, payload: dict):
        await asyncio.sleep(0)
        self.sent.append(payload)

    async def close(self, code: int, reason: str):
        self.closed.append((code, reason))


class _BlockingSocket(_FakeSocket):
    def __init__(self):
        super().__init__()
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()

    async def send_json(self, payload: dict):
        self.send_started.set()
        await self.release_send.wait()
        self.sent.append(payload)


async def _enrolled_service():
    service = ConnectorGateway()
    pairing = await service.create_pairing(None, "test computer")
    await service.approve_pairing(pairing["user_code"], "owner-1")
    credential = await service.exchange_pairing(pairing["device_code"])
    await service.update_inventory(
        credential["device_id"],
        [
            RuntimeInventoryItem(
                runtime_id="codex-default",
                kind="codex",
                display_name="Codex CLI",
                executable_path="codex",
                capabilities=[action.value for action in CommandAction],
                task_enabled=True,
                authentication_status="configured",
                arena_compatible=True,
                arena_isolation="read_only_ephemeral_schema",
                local_execution_ready=True,
            )
        ],
    )
    binding = await service.create_binding(
        credential["device_id"], "codex-default", None, None
    )
    return service, credential, binding


def test_typed_arena_task_rejects_detected_but_not_execution_ready_runtime():
    async def scenario():
        service, credential, binding = await _enrolled_service()
        runtime = service.devices[credential["device_id"]]["runtimes"][0]
        runtime["arena_compatible"] = False
        runtime["local_execution_ready"] = True
        runtime["readiness_issues"] = ["arena_profile_unsupported"]

        start = await service.queue_command(
            binding["binding_id"],
            CommandAction.SESSION_START,
            {"working_directory": "E:\\arena-workspace"},
            "start-not-ready-session",
            300,
        )
        await service.acknowledge_command(
            credential["device_id"],
            {
                "command_id": start["command_id"],
                "status": "succeeded",
                "result": {"session_id": "not-ready-session"},
            },
        )
        task = _arena_decide_task()
        with pytest.raises(ConnectorError, match="not ready for Arena execution"):
            await service.queue_command(
                binding["binding_id"],
                CommandAction.TASK_DISPATCH,
                {"session_id": "not-ready-session", "task": task},
                task["idempotencyKey"],
                300,
            )

    asyncio.run(scenario())


def test_existing_binding_can_freeze_working_directory_once():
    async def scenario():
        service, credential, binding = await _enrolled_service()

        upgraded = await service.create_binding(
            credential["device_id"],
            "codex-default",
            None,
            None,
            "E:\\arena-workspace",
        )

        assert upgraded["binding_id"] == binding["binding_id"]
        assert upgraded["working_directory"] == "E:\\arena-workspace"
        with pytest.raises(ConnectorError, match="working directory"):
            await service.create_binding(
                credential["device_id"],
                "codex-default",
                None,
                None,
                "E:\\different-workspace",
            )

    asyncio.run(scenario())


def test_typed_arena_task_dispatch_is_accepted_without_free_prompt():
    async def scenario():
        service, credential, binding = await _enrolled_service()
        device_id = credential["device_id"]
        binding_id = binding["binding_id"]
        start = await service.queue_command(
            binding_id,
            CommandAction.SESSION_START,
            {"working_directory": "E:\\arena-workspace"},
            "start-arena-session",
            300,
        )
        await service.acknowledge_command(
            device_id,
            {
                "command_id": start["command_id"],
                "status": "running",
                "result": {"session_id": "arena-session-1"},
            },
        )

        task = _arena_decide_task()
        dispatch = await service.queue_command(
            binding_id,
            CommandAction.TASK_DISPATCH,
            {"session_id": "arena-session-1", "task": task},
            task["idempotencyKey"],
            300,
        )

        assert dispatch["status"] == "queued"
        assert dispatch["payload"] == {
            "session_id": "arena-session-1",
            "task": task,
        }
        assert "prompt" not in dispatch["payload"]

    asyncio.run(scenario())


def test_terminal_agent_task_result_is_received_separately_from_command_ack():
    async def scenario():
        service, credential, binding = await _enrolled_service()
        device_id = credential["device_id"]
        binding_id = binding["binding_id"]
        start = await service.queue_command(
            binding_id,
            CommandAction.SESSION_START,
            {"working_directory": "E:\\arena-workspace"},
            "start-arena-result-session",
            300,
        )
        await service.acknowledge_command(
            device_id,
            {
                "command_id": start["command_id"],
                "status": "running",
                "result": {"session_id": "arena-result-session"},
            },
        )
        task = _arena_decide_task()
        dispatch = await service.queue_command(
            binding_id,
            CommandAction.TASK_DISPATCH,
            {"session_id": "arena-result-session", "task": task},
            task["idempotencyKey"],
            300,
        )

        receipt = await service.submit_agent_task_result(
            device_id,
            {
                "binding_id": binding_id,
                "binding_epoch": binding["binding_epoch"],
                "result": {
                    "schemaVersion": "arena.agent-result.v1",
                    "resultId": "result-arena-decide-1",
                    "taskId": task["taskId"],
                    "status": "succeeded",
                    "action": {"action": "buy", "good": "grain"},
                },
            },
        )

        assert receipt["disposition"] == "accepted"
        assert receipt["task_id"] == task["taskId"]
        commands = await service.list_commands(binding_id)
        current = next(
            item for item in commands if item["command_id"] == dispatch["command_id"]
        )
        assert current["status"] == "queued"
        assert current["result"] is None

    asyncio.run(scenario())


def test_typed_arena_task_connector_restart_retry_is_capped_at_two_attempts():
    async def scenario():
        service, credential, binding = await _enrolled_service()
        device_id = credential["device_id"]
        binding_id = binding["binding_id"]
        task = _arena_decide_task()

        first_start = await service.queue_command(
            binding_id,
            CommandAction.SESSION_START,
            {"working_directory": "E:\\arena-workspace"},
            "restart-attempt-session-1",
            300,
        )
        await service.acknowledge_command(
            device_id,
            {
                "command_id": first_start["command_id"],
                "status": "succeeded",
                "result": {"session_id": "restart-attempt-session-1"},
            },
        )
        first_dispatch = await service.queue_command(
            binding_id,
            CommandAction.TASK_DISPATCH,
            {
                "session_id": "restart-attempt-session-1",
                "task": task,
            },
            task["idempotencyKey"],
            300,
        )
        await service.acknowledge_command(
            device_id,
            {
                "command_id": first_dispatch["command_id"],
                "status": "failed",
                "error": {
                    "code": "connector_restarted",
                    "message": "first Connector process stopped",
                },
            },
        )

        second_start = await service.queue_command(
            binding_id,
            CommandAction.SESSION_START,
            {"working_directory": "E:\\arena-workspace"},
            "restart-attempt-session-2",
            300,
        )
        await service.acknowledge_command(
            device_id,
            {
                "command_id": second_start["command_id"],
                "status": "succeeded",
                "result": {"session_id": "restart-attempt-session-2"},
            },
        )
        second_dispatch = await service.queue_command(
            binding_id,
            CommandAction.TASK_DISPATCH,
            {
                "session_id": "restart-attempt-session-2",
                "task": task,
            },
            task["idempotencyKey"],
            300,
        )
        assert second_dispatch["command_id"] != first_dispatch["command_id"]
        await service.acknowledge_command(
            device_id,
            {
                "command_id": second_dispatch["command_id"],
                "status": "failed",
                "error": {
                    "code": "connector_restarted",
                    "message": "second Connector process stopped",
                },
            },
        )

        third_start = await service.queue_command(
            binding_id,
            CommandAction.SESSION_START,
            {"working_directory": "E:\\arena-workspace"},
            "restart-attempt-session-3",
            300,
        )
        await service.acknowledge_command(
            device_id,
            {
                "command_id": third_start["command_id"],
                "status": "succeeded",
                "result": {"session_id": "restart-attempt-session-3"},
            },
        )
        with pytest.raises(ConnectorError, match="already used"):
            await service.queue_command(
                binding_id,
                CommandAction.TASK_DISPATCH,
                {
                    "session_id": "restart-attempt-session-3",
                    "task": task,
                },
                task["idempotencyKey"],
                300,
            )

    asyncio.run(scenario())


def test_websocket_accepts_agent_task_result_as_its_own_message_type():
    client = TestClient(create_app(connector_demo_enabled=True))
    _, credential = _enroll(client)
    service = client.app.state.connector_gateway

    async def setup():
        await service.update_inventory(
            credential["device_id"],
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex CLI",
                    executable_path="codex",
                    capabilities=[action.value for action in CommandAction],
                    task_enabled=True,
                    authentication_status="configured",
                    arena_compatible=True,
                    arena_isolation="read_only_ephemeral_schema",
                    local_execution_ready=True,
                )
            ],
        )
        binding = await service.create_binding(
            credential["device_id"],
            "codex-default",
            None,
            None,
        )
        start = await service.queue_command(
            binding["binding_id"],
            CommandAction.SESSION_START,
            {"working_directory": "E:\\arena-workspace"},
            "start-arena-websocket-session",
            300,
        )
        await service.acknowledge_command(
            credential["device_id"],
            {
                "command_id": start["command_id"],
                "status": "running",
                "result": {"session_id": "arena-websocket-session"},
            },
        )
        task = _arena_decide_task()
        await service.queue_command(
            binding["binding_id"],
            CommandAction.TASK_DISPATCH,
            {"session_id": "arena-websocket-session", "task": task},
            task["idempotencyKey"],
            300,
        )
        return binding, task

    binding, task = asyncio.run(setup())
    with client.websocket_connect(
        f"/api/connectors/ws?device_id={credential['device_id']}",
        headers={"Authorization": f"Device {credential['device_token']}"},
    ) as socket:
        assert socket.receive_json()["type"] == "welcome"
        delivered = socket.receive_json()
        assert delivered["type"] == "command"
        assert delivered["payload"]["payload"]["task"]["taskId"] == task["taskId"]

        socket.send_json(
            {
                "type": "agent_task.result",
                "protocol_version": "1.0",
                "message_id": "agent-task-result-1",
                "payload": {
                    "binding_id": binding["binding_id"],
                    "binding_epoch": binding["binding_epoch"],
                    "result": {
                        "schemaVersion": "arena.agent-result.v1",
                        "resultId": "result-arena-decide-1",
                        "taskId": task["taskId"],
                        "status": "succeeded",
                        "action": {"action": "buy", "good": "grain"},
                    },
                },
            }
        )
        acknowledged = socket.receive_json()

        assert acknowledged["type"] == "agent_task.result.ack"
        assert acknowledged["payload"]["disposition"] == "accepted"
        assert acknowledged["payload"]["task_id"] == task["taskId"]


def test_single_sender_and_revocation_cover_every_authenticated_socket():
    async def scenario():
        service, credential, binding = await _enrolled_service()
        device_id = credential["device_id"]
        command = await service.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "probe-once",
            300,
        )

        first = _FakeSocket()
        await service.connect_device(device_id, first)
        await asyncio.gather(
            service.deliver_pending(device_id),
            service.deliver_pending(device_id),
        )
        sent_commands = [
            payload
            for payload in first.sent
            if payload.get("message_id") == command["command_id"]
        ]
        assert len(sent_commands) == 1

        second = _FakeSocket()
        await service.connect_device(device_id, second)
        assert first.closed[-1][0] == 4409
        with pytest.raises(ConnectorError, match="no longer an active"):
            await service.assert_active_connection(device_id, first)

        await service.revoke_device(device_id, "owner-1")
        assert any(code == 4403 for code, _ in first.closed)
        assert any(code == 4403 for code, _ in second.closed)
        with pytest.raises(ConnectorError, match="revoked"):
            await service.heartbeat(device_id, {})

    asyncio.run(scenario())


def test_late_task_failure_cannot_overwrite_stopped_session_state():
    async def scenario():
        service, credential, binding = await _enrolled_service()
        device_id = credential["device_id"]
        binding_id = binding["binding_id"]

        start = await service.queue_command(
            binding_id,
            CommandAction.SESSION_START,
            {"working_directory": "E:\\workspace"},
            "start-state-test",
            300,
        )
        await service.acknowledge_command(
            device_id,
            {
                "command_id": start["command_id"],
                "status": "running",
                "result": {"session_id": "session-state-test"},
            },
        )
        with pytest.raises(ConnectorError, match="does not belong"):
            await service.queue_command(
                binding_id,
                CommandAction.TASK_DISPATCH,
                {
                    "session_id": "another-binding-session",
                    "prompt": "must not cross the binding boundary",
                    "request_id": "cross-binding-request",
                },
                "cross-binding-dispatch",
                300,
            )
        dispatch = await service.queue_command(
            binding_id,
            CommandAction.TASK_DISPATCH,
            {
                "session_id": "session-state-test",
                "prompt": "work",
                "request_id": "request-state-test",
            },
            "dispatch-state-test",
            300,
        )
        await service.acknowledge_command(
            device_id,
            {
                "command_id": dispatch["command_id"],
                "status": "running",
                "result": {"task_id": "task-state-test"},
            },
        )
        stop = await service.queue_command(
            binding_id,
            CommandAction.SESSION_STOP,
            {"session_id": "session-state-test"},
            "stop-state-test",
            300,
        )
        await service.acknowledge_command(
            device_id,
            {"command_id": stop["command_id"], "status": "succeeded"},
        )
        await service.acknowledge_command(
            device_id,
            {
                "command_id": dispatch["command_id"],
                "status": "failed",
                "result": {"task_id": "task-state-test"},
                "error": {"code": "task_cancelled", "message": "cancelled by stop"},
            },
        )

        current = (await service.list_bindings(device_id))[0]
        assert current["status"] == "stopped"
        assert current["last_task_id"] is None

    asyncio.run(scenario())


def test_connector_restart_invalidates_process_local_session_projection():
    async def scenario():
        service, credential, binding = await _enrolled_service()
        device_id = credential["device_id"]
        binding_id = binding["binding_id"]
        await service.apply_hello(
            device_id,
            {
                "protocol_version": "1.0",
                "connector_version": "0.1.0",
                "started_at": "2026-07-23T10:00:00Z",
            },
        )
        start = await service.queue_command(
            binding_id,
            CommandAction.SESSION_START,
            {"working_directory": "E:\\workspace"},
            "restart-state-test",
            300,
        )
        await service.acknowledge_command(
            device_id,
            {
                "command_id": start["command_id"],
                "status": "running",
                "result": {"session_id": "process-local-session"},
            },
        )

        await service.apply_hello(
            device_id,
            {
                "protocol_version": "1.0",
                "connector_version": "0.1.0",
                "started_at": "2026-07-23T10:05:00Z",
            },
        )
        await service.acknowledge_command(
            device_id,
            {
                "command_id": start["command_id"],
                "status": "succeeded",
                "result": {"session_id": "process-local-session"},
            },
        )

        current = (await service.list_bindings(device_id))[0]
        assert current["status"] == "degraded"
        assert current["last_session_id"] is None
        assert any(
            item["action"] == "device.connector_restarted"
            for item in await service.list_audit()
        )

    asyncio.run(scenario())


def test_connection_handover_requeues_command_sent_during_replacement():
    async def scenario():
        service, credential, binding = await _enrolled_service()
        device_id = credential["device_id"]
        command = await service.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "handover-probe",
            300,
        )
        old_socket = _BlockingSocket()
        old_generation = await service.connect_device(device_id, old_socket)

        old_delivery = asyncio.create_task(service.deliver_pending(device_id))
        await old_socket.send_started.wait()
        new_socket = _FakeSocket()
        handover = asyncio.create_task(service.connect_device(device_id, new_socket))
        await asyncio.sleep(0)
        assert not handover.done()

        old_socket.release_send.set()
        await old_delivery
        new_generation = await handover
        assert new_generation > old_generation
        assert service.commands[command["command_id"]]["status"] == "queued"

        await service.deliver_pending(device_id)
        assert any(
            payload.get("message_id") == command["command_id"]
            for payload in new_socket.sent
        )
        assert service.commands[command["command_id"]]["status"] == "delivered"

        with pytest.raises(ConnectorError, match="no longer an active"):
            await service.acknowledge_command(
                device_id,
                {
                    "command_id": command["command_id"],
                    "status": "succeeded",
                },
                expected_generation=old_generation,
            )
        assert service.commands[command["command_id"]]["status"] == "delivered"

        with pytest.raises(ConnectorError, match="no longer an active"):
            await service.append_runtime_event(
                device_id,
                {
                    "event_id": "stale-socket-event",
                    "binding_id": binding["binding_id"],
                    "sequence": 1,
                    "event_type": "task.completed",
                    "data": {},
                },
                expected_generation=old_generation,
            )
        assert service.events == []

    asyncio.run(scenario())
