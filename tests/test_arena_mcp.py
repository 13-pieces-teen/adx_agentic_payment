from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena_agent_contracts import (
    AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
    AgentTaskResultV1,
    ArenaAgentTaskV1,
    PassAction,
)
from arena_core import ConnectorTaskRoute
from arena_core.models import (
    ResultSubmissionReceipt,
    SubmissionDisposition,
    TaskStatus,
)
from arena_mcp import (
    ArenaMCPBrokerError,
    ArenaTaskBroker,
    ExecutionTokenCodec,
)
from arena_mcp.auth import ExecutionTokenError
from arena_mcp.server import (
    MCP_PROTOCOL_VERSION,
    create_arena_mcp_router,
)
from connector_gateway.arena_notifier import ConnectorArenaTaskNotifier
from connector_gateway.service import ConnectorGateway


SECRET = "mcp-test-secret-that-is-longer-than-thirty-two-bytes"


def _task() -> ArenaAgentTaskV1:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=60)
    return ArenaAgentTaskV1.model_validate(
        {
            "taskId": "task-mcp-1",
            "kind": "arena.decide",
            "schemaVersion": "arena.agent-task.v1",
            "gameId": "game-1",
            "roundId": "round-1",
            "gameAgentId": "game-agent-1",
            "negotiationId": None,
            "deadlineAt": deadline.isoformat(),
            "idempotencyKey": "game-1:round-1:game-agent-1:decide",
            "inputHash": "sha256:" + ("0" * 64),
            "input": {
                "phase": "decide",
                "gameId": "game-1",
                "roundId": "round-1",
                "roundIndex": 1,
                "cash": "20.000000",
                "holdings": {},
                "market": {"grain": "2.000000"},
                "events": [],
                "reputation": {"failedNegotiations": 0},
                "limits": {
                    "allowedActions": ["buy", "sell", "pass"],
                    "allowedGoods": ["grain"],
                },
                "completedActions": [],
                "completedTrades": [],
                "goods": [],
                "marketActivity": [],
                "deadlineAt": deadline.isoformat(),
            },
        }
    )


class _Gateway:
    def __init__(self) -> None:
        self.binding = {
            "binding_id": "binding-1",
            "device_id": "device-1",
            "binding_epoch": 7,
            "agent_id": "agent-1",
            "runtime_id": "codex",
            "last_session_id": "session-1",
        }

    async def authenticate_device(self, device_id: str, token: str):
        if device_id != "device-1" or token != "device-token":
            raise RuntimeError("invalid")
        return {"device_id": device_id}

    async def list_bindings(self, device_id: str | None = None):
        if device_id not in {None, "device-1"}:
            return []
        return [dict(self.binding)]


class _Broker:
    def __init__(self) -> None:
        self.claims: list[str] = []

    async def claim(self, *, principal, task_id: str):
        self.claims.append(task_id)
        return {
            "leaseId": principal.worker_id,
            "task": _task().model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            ),
            "execution": {
                "bindingId": principal.binding_id,
                "bindingEpoch": principal.binding_epoch,
                "agentId": "agent-1",
                "runtimeId": "codex",
                "sessionId": "session-1",
            },
        }

    async def status(self, **_: Any):
        return {
            "taskId": "task-mcp-1",
            "status": "leased",
            "deadlineAt": "2026-07-30T00:00:00Z",
            "leaseExpiresAt": None,
            "hasAuthoritativeResult": False,
            "resultDisposition": None,
        }

    async def submit(self, **_: Any):
        raise AssertionError("not used")

    async def release(self, **_: Any):
        raise AssertionError("not used")

    async def sync(self, **_: Any):
        return {"tasks": [], "hasMore": False, "nextCursor": None}


def _mcp_headers(
    token: str,
    method: str,
    *,
    name: str | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


def _rpc(
    method: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved = dict(params or {})
    resolved["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": {
            "name": "test-client",
            "version": "1.0",
        },
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    return {
        "jsonrpc": "2.0",
        "id": "request-1",
        "method": method,
        "params": resolved,
    }


def _client():
    gateway = _Gateway()
    broker = _Broker()
    codec = ExecutionTokenCodec(SECRET)
    token, _ = codec.issue(
        device_id="device-1",
        binding_id="binding-1",
        binding_epoch=7,
    )
    app = FastAPI()
    app.include_router(
        create_arena_mcp_router(
            broker=broker,
            token_codec=codec,
            gateway=gateway,
            allowed_origins={"https://arena.example"},
        )
    )
    return TestClient(app), broker, token


def test_execution_token_is_binding_scoped_tamper_evident_and_expiring():
    current = 1000.0
    codec = ExecutionTokenCodec(SECRET, clock=lambda: current)
    token, claims = codec.issue(
        device_id="device-1",
        binding_id="binding-1",
        binding_epoch=3,
        ttl_seconds=30,
    )

    assert codec.decode(token) == claims
    with pytest.raises(ExecutionTokenError):
        codec.decode(token[:-1] + ("A" if token[-1] != "A" else "B"))

    current = 1030.0
    with pytest.raises(ExecutionTokenError, match="expired"):
        codec.decode(token)


def test_token_exchange_requires_device_authority_and_freezes_epoch():
    client, _, _ = _client()
    response = client.post(
        "/api/connectors/mcp/token",
        headers={"Authorization": "Device device-token"},
        json={"deviceId": "device-1", "bindingId": "binding-1"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["binding_epoch"] == 7
    assert response.json()["token_type"] == "Bearer"


def test_stateless_mcp_discovery_and_tool_list_use_per_request_metadata():
    client, _, token = _client()
    discovery = client.post(
        "/mcp",
        headers=_mcp_headers(token, "server/discover"),
        json=_rpc("server/discover"),
    )
    tools = client.post(
        "/mcp",
        headers=_mcp_headers(token, "tools/list"),
        json=_rpc("tools/list"),
    )

    assert discovery.status_code == 200
    assert discovery.json()["result"]["supportedVersions"] == [MCP_PROTOCOL_VERSION]
    assert (
        discovery.json()["result"]["_meta"]["io.modelcontextprotocol/serverInfo"][
            "name"
        ]
        == "arena402_mcp"
    )
    listed = tools.json()["result"]["tools"]
    assert [item["name"] for item in listed] == [
        "arena_claim_agent_task",
        "arena_get_agent_task_status",
        "arena_submit_agent_task_result",
        "arena_release_agent_task",
        "arena_sync_agent_tasks",
    ]
    assert all("annotations" in item for item in listed)


def test_mcp_claim_returns_structured_content_and_validates_mirrored_headers():
    client, broker, token = _client()
    name = "arena_claim_agent_task"
    body = _rpc(
        "tools/call",
        params={"name": name, "arguments": {"taskId": "task-mcp-1"}},
    )
    response = client.post(
        "/mcp",
        headers=_mcp_headers(token, "tools/call", name=name),
        json=body,
    )
    mismatched = client.post(
        "/mcp",
        headers=_mcp_headers(
            token,
            "tools/call",
            name="arena_sync_agent_tasks",
        ),
        json=body,
    )

    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    assert (
        response.json()["result"]["structuredContent"]["task"]["taskId"] == "task-mcp-1"
    )
    assert broker.claims == ["task-mcp-1"]
    assert mismatched.status_code == 400
    assert mismatched.json()["error"]["code"] == -32020


def test_mcp_rejects_missing_meta_and_untrusted_origin():
    client, _, token = _client()
    missing_meta = client.post(
        "/mcp",
        headers=_mcp_headers(token, "server/discover"),
        json={
            "jsonrpc": "2.0",
            "id": "request-1",
            "method": "server/discover",
            "params": {},
        },
    )
    bad_origin_headers = _mcp_headers(token, "server/discover")
    bad_origin_headers["Origin"] = "https://attacker.example"
    bad_origin = client.post(
        "/mcp",
        headers=bad_origin_headers,
        json=_rpc("server/discover"),
    )

    assert missing_meta.status_code == 400
    assert missing_meta.json()["error"]["code"] == -32602
    assert bad_origin.status_code == 403


class _BrokerRepository:
    def __init__(self, route: ConnectorTaskRoute) -> None:
        self.route = route
        self.released = False

    async def claim_connector_task(self, **kwargs):
        assert kwargs["connector_binding_id"] == "binding-1"
        assert kwargs["connector_binding_epoch"] == 7
        self.route = ConnectorTaskRoute(
            task=self.route.task,
            connector_binding_id=self.route.connector_binding_id,
            connector_binding_epoch=self.route.connector_binding_epoch,
            status=TaskStatus.LEASED,
            leased_by=kwargs["worker_id"],
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        return self.route

    async def get_connector_task_route(self, **kwargs):
        if kwargs["task_id"] != self.route.task.task_id:
            return None
        if kwargs["connector_binding_epoch"] != self.route.connector_binding_epoch:
            return None
        return self.route

    async def list_connector_task_routes(self, **_: Any):
        return (self.route,)

    async def release_connector_task(self, **kwargs):
        self.released = kwargs["worker_id"] == self.route.leased_by
        return self.released

    async def get_result_for_task(self, task_id: str):
        assert task_id == self.route.task.task_id
        return None


class _ResultSink:
    def __init__(self) -> None:
        self.results: list[AgentTaskResultV1] = []

    async def submit(self, result: AgentTaskResultV1):
        self.results.append(result)
        return ResultSubmissionReceipt(
            task_id=result.task_id,
            disposition=SubmissionDisposition.ACCEPTED,
            authoritative_result_id="runtime:" + ("a" * 64),
            task_status=TaskStatus.COMPLETED,
            result_received_at=datetime.now(timezone.utc),
        )


def test_task_broker_keeps_binding_epoch_and_result_sink_authoritative():
    async def scenario():
        task = _task()
        route = ConnectorTaskRoute(
            task=task,
            connector_binding_id="binding-1",
            connector_binding_epoch=7,
            status=TaskStatus.QUEUED,
            leased_by=None,
            lease_expires_at=None,
        )
        repository = _BrokerRepository(route)
        sink = _ResultSink()
        gateway = _Gateway()
        broker = ArenaTaskBroker(
            repository=repository,
            result_sink=sink,
            gateway=gateway,
        )
        _, principal = ExecutionTokenCodec(SECRET).issue(
            device_id="device-1",
            binding_id="binding-1",
            binding_epoch=7,
        )

        claim = await broker.claim(
            principal=principal,
            task_id=task.task_id,
        )
        result = AgentTaskResultV1(
            result_id="result-mcp-1",
            task_id=task.task_id,
            schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
            status="succeeded",
            action=PassAction(action="pass"),
        )
        receipt = await broker.submit(
            principal=principal,
            result=result,
        )

        assert claim["execution"]["bindingEpoch"] == 7
        assert receipt["disposition"] == "accepted"
        assert sink.results == [result]

        gateway.binding["binding_epoch"] = 8
        with pytest.raises(ArenaMCPBrokerError, match="stale binding epoch"):
            await broker.status(
                principal=principal,
                task_id=task.task_id,
            )

    asyncio.run(scenario())


class _WakeRepository:
    def __init__(self, route: ConnectorTaskRoute) -> None:
        self.route = route

    async def list_connector_task_wakes(self, *, limit: int):
        assert limit == 100
        return (self.route,)


class _Socket:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_json(self, value: dict[str, Any]) -> None:
        self.messages.append(value)


def test_wss_notifier_sends_only_safe_task_hint_and_throttles_replay():
    async def scenario():
        task = _task()
        route = ConnectorTaskRoute(
            task=task,
            connector_binding_id="binding-1",
            connector_binding_epoch=7,
            status=TaskStatus.QUEUED,
            leased_by=None,
            lease_expires_at=None,
        )
        gateway = ConnectorGateway()
        gateway.devices["device-1"] = {
            "device_id": "device-1",
            "owner_id": "user-1",
            "revoked_at": None,
            "binding_epoch": 7,
            "_connection_generation": 1,
            "outbound_sequence": 0,
            "runtimes": [],
        }
        gateway.bindings["binding-1"] = {
            "binding_id": "binding-1",
            "device_id": "device-1",
            "runtime_id": "codex",
            "agent_id": "agent-1",
            "binding_epoch": 7,
            "last_session_id": "session-1",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        socket = _Socket()
        generation = await gateway.connect_device("device-1", socket)
        await gateway.mark_transport_ready("device-1", generation)
        now = 10.0
        notifier = ConnectorArenaTaskNotifier(
            repository=_WakeRepository(route),
            gateway=gateway,
            monotonic=lambda: now,
        )

        assert await notifier.run_once() == 1
        assert await notifier.run_once() == 0
        assert len(socket.messages) == 1
        message = socket.messages[0]
        assert message["type"] == "task.available"
        assert set(message["payload"]) == {
            "wake_id",
            "task_id",
            "binding_id",
            "binding_epoch",
            "deadline_at",
        }

    asyncio.run(scenario())


def test_wss_notifier_does_not_create_sessions_on_the_data_plane():
    async def scenario():
        route = ConnectorTaskRoute(
            task=_task(),
            connector_binding_id="binding-1",
            connector_binding_epoch=7,
            status=TaskStatus.QUEUED,
            leased_by=None,
            lease_expires_at=None,
        )

        class Gateway:
            async def list_bindings(self):
                return [
                    {
                        "binding_id": "binding-1",
                        "binding_epoch": 7,
                        "last_session_id": None,
                        "working_directory": "C:/arena",
                    }
                ]

            async def notify_task_available(self, binding_id, payload):
                raise AssertionError("wake must wait for a managed session")

            async def queue_command(self, *args, **kwargs):
                raise AssertionError("WSS worker must not create Commands")

        notifier = ConnectorArenaTaskNotifier(
            repository=_WakeRepository(route),
            gateway=Gateway(),
            manage_sessions=False,
        )
        assert await notifier.run_once() == 0

    asyncio.run(scenario())
