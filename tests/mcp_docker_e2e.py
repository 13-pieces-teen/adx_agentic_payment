"""Isolated Docker E2E for the WSS wake + stateless MCP task path.

The stack must expose the API and PostgreSQL on loopback. This test creates a
temporary user/device/binding, emulates only the Connector control frames
needed to establish a managed session, creates one real PostgreSQL AgentTask
through the production repository, and verifies:

    WSS task.available -> MCP sync -> claim -> submit -> authoritative status

It does not invoke Claude Code/Codex, start settlement workers, or write to a
chain.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import httpx
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arena_agent_contracts import (
    AGENT_TASK_SCHEMA_VERSION_V1,
    ArenaAgentTaskV1,
    ArenaDecideInputV1,
    ArenaDecideLimitsV1,
    ArenaReputationV1,
)
from arena_core import PostgresArenaCoreRepository
from arena_core.hashing import sha256_identifier


API_BASE = os.getenv("ADX_MCP_E2E_API_BASE", "http://127.0.0.1:18000")
WS_BASE = os.getenv("ADX_MCP_E2E_WS_BASE", "ws://127.0.0.1:18000")
ADMIN_URL = os.getenv(
    "ADX_MCP_E2E_ADMIN_URL",
    "postgresql://arena402_admin:arena402-local-admin-password"
    "@127.0.0.1:55433/arena402",
)
CORE_URL = os.getenv(
    "ADX_MCP_E2E_CORE_URL",
    "postgresql://adx_arena_core_login:arena402-local-core-password"
    "@127.0.0.1:55433/arena402",
)
INVITE = "arena402-local-development-invite"
MCP_VERSION = "2026-07-28"


def _receive_type(websocket, expected: str) -> dict:
    message = json.loads(websocket.recv(timeout=15))
    if message.get("type") != expected:
        raise AssertionError(f"expected WSS {expected}, received {message!r}")
    return message


def _send_connector_frame(websocket, frame_type: str, message_id: str, payload: dict):
    websocket.send(
        json.dumps(
            {
                "type": frame_type,
                "protocol_version": "1.0",
                "message_id": message_id,
                "payload": payload,
            }
        )
    )


def _mcp_call(
    client: httpx.Client,
    token: str,
    method: str,
    *,
    name: str | None = None,
    arguments: dict | None = None,
) -> dict:
    params: dict = {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": MCP_VERSION,
            "io.modelcontextprotocol/clientInfo": {
                "name": "arena402-docker-e2e",
                "version": "1.0",
            },
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    }
    if name is not None:
        params.update({"name": name, "arguments": arguments or {}})
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_VERSION,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": f"e2e-{uuid.uuid4().hex}",
            "method": method,
            "params": params,
        },
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise AssertionError(f"MCP protocol error: {payload['error']!r}")
    result = payload["result"]
    if result.get("isError"):
        raise AssertionError(f"MCP tool error: {result!r}")
    return result


async def _create_task(
    *,
    owner_id: str,
    agent_id: str,
    connector_binding_id: str,
) -> ArenaAgentTaskV1:
    suffix = uuid.uuid4().hex[:12]
    game_id = f"mcp-game-{suffix}"
    round_id = f"mcp-round-{suffix}"
    game_agent_id = f"mcp-game-agent-{suffix}"
    task_id = f"mcp-task-{suffix}"
    config = {
        "runtime_kind": "connector",
        "strategy": "docker-e2e-pass",
    }
    config_hash = sha256_identifier(config)
    deadline = datetime.now(timezone.utc) + timedelta(minutes=2)
    participant_view = ArenaDecideInputV1(
        phase="decide",
        game_id=game_id,
        round_id=round_id,
        round_index=1,
        cash="20.000000",
        holdings={"grain": 0},
        market={"grain": "2.000000"},
        reputation=ArenaReputationV1(failed_negotiations=0),
        limits=ArenaDecideLimitsV1(
            allowed_actions=["buy", "sell", "pass"],
            allowed_goods=["grain"],
        ),
        deadline_at=deadline,
    )
    task = ArenaAgentTaskV1(
        task_id=task_id,
        kind="arena.decide",
        schema_version=AGENT_TASK_SCHEMA_VERSION_V1,
        game_id=game_id,
        round_id=round_id,
        game_agent_id=game_agent_id,
        deadline_at=deadline,
        idempotency_key=f"{game_id}:{round_id}:{game_agent_id}:decide",
        input_hash=sha256_identifier(participant_view),
        input=participant_view,
    )

    admin = await asyncpg.connect(ADMIN_URL)
    core = PostgresArenaCoreRepository(CORE_URL)
    try:
        runtime_binding_id = await admin.fetchval(
            """
            SELECT runtime_binding_id
            FROM arena_runtime_bindings
            WHERE connector_binding_id = $1
              AND route_status = 'ready'
              AND disabled_at IS NULL
            """,
            connector_binding_id,
        )
        if runtime_binding_id is None:
            raise AssertionError("Connector Arena runtime route is not ready")
        await admin.execute(
            """
            INSERT INTO games (
                game_id, status, action_timeout_ms, config_snapshot
            )
            VALUES ($1, 'open', 120000, '{}'::jsonb)
            """,
            game_id,
        )
        await admin.execute(
            """
            INSERT INTO rounds (
                round_id, game_id, round_index, phase, deadline_at
            )
            VALUES ($1, $2, 1, 'decide', $3)
            """,
            round_id,
            game_id,
            deadline,
        )
        await admin.execute(
            """
            INSERT INTO game_agents (
                game_agent_id,
                game_id,
                user_id,
                agent_id,
                runtime_binding_id,
                config_snapshot,
                config_hash,
                status,
                initial_cash_atomic,
                initial_inventory
            )
            VALUES (
                $1, $2, $3, $4, $5, $6::jsonb, $7,
                'active', 20000000, '{"grain":0}'::jsonb
            )
            """,
            game_agent_id,
            game_id,
            owner_id,
            agent_id,
            runtime_binding_id,
            json.dumps(config),
            config_hash,
        )
        await core.initialize()
        await core.create_task(
            task=task,
            config_snapshot=config,
            config_hash=config_hash,
            created_at=datetime.now(timezone.utc),
        )
        return task
    finally:
        await core.close()
        await admin.close()


async def _task_evidence(task_id: str) -> dict:
    connection = await asyncpg.connect(ADMIN_URL)
    try:
        row = await connection.fetchrow(
            """
            SELECT
                t.status,
                t.leased_by,
                COUNT(r.result_id) AS result_count
            FROM arena_agent_tasks AS t
            LEFT JOIN arena_agent_task_results AS r
              ON r.task_id = t.task_id
            WHERE t.task_id = $1
            GROUP BY t.status, t.leased_by
            """,
            task_id,
        )
        if row is None:
            raise AssertionError("Arena task disappeared")
        return dict(row)
    finally:
        await connection.close()


def main() -> None:
    suffix = uuid.uuid4().hex[:10]
    with httpx.Client(base_url=API_BASE, timeout=15) as client:
        health = client.get("/api/health")
        health.raise_for_status()
        if health.json().get("arena_mcp") is not True:
            raise AssertionError(f"Arena MCP is not enabled: {health.json()!r}")

        registration = client.post(
            "/api/auth/register",
            json={
                "invite_code": INVITE,
                "username": f"mcp_e2e_{suffix}",
                "password": "correct horse battery staple",
            },
        )
        registration.raise_for_status()
        session = registration.json()
        owner_id = session["user"]["user_id"]
        csrf = session["csrf_token"]

        pairing = client.post(
            "/api/connectors/pairings",
            json={"device_name": "MCP Docker E2E"},
        )
        pairing.raise_for_status()
        pairing_data = pairing.json()
        approval = client.post(
            f"/api/connectors/pairings/{pairing_data['user_code']}/approve",
            headers={"X-CSRF-Token": csrf},
            json={},
        )
        approval.raise_for_status()
        exchange = client.post(
            "/api/connectors/pairings/exchange",
            json={"device_code": pairing_data["device_code"]},
        )
        exchange.raise_for_status()
        credential = exchange.json()
        device_id = credential["device_id"]

        websocket_path = str(credential["ws_url"])
        separator = "&" if "?" in websocket_path else "?"
        with connect(
            f"{WS_BASE}{websocket_path}{separator}device_id={device_id}",
            additional_headers={
                "Authorization": f"Device {credential['device_token']}",
            },
            open_timeout=15,
        ) as websocket:
            _receive_type(websocket, "welcome")
            _send_connector_frame(
                websocket,
                "hello",
                "mcp-e2e-hello",
                {
                    "protocol_version": "1.0",
                    "connector_version": "mcp-docker-e2e",
                    "platform": "test/amd64",
                    "hostname": "mcp-docker-e2e",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            _receive_type(websocket, "ack")
            _send_connector_frame(
                websocket,
                "inventory.snapshot",
                "mcp-e2e-inventory",
                {
                    "host": {
                        "hostname": "mcp-docker-e2e",
                        "os": "test",
                        "architecture": "amd64",
                        "connector_version": "mcp-docker-e2e",
                    },
                    "runtimes": [
                        {
                            "runtime_id": "codex-e2e",
                            "kind": "codex",
                            "display_name": "Codex E2E Stub",
                            "executable_path": "/test/codex",
                            "version": "e2e",
                            "available": True,
                            "capabilities": [
                                "session.start",
                                "task.dispatch",
                            ],
                            "auth_modes": ["test"],
                            "task_enabled": True,
                            "authentication_status": "configured",
                            "arena_compatible": True,
                            "arena_isolation": "read_only_ephemeral_schema",
                            "local_execution_ready": True,
                            "readiness_issues": [],
                        }
                    ],
                },
            )
            _receive_type(websocket, "ack")

            binding_response = client.post(
                f"/api/connectors/devices/{device_id}/bindings",
                headers={"X-CSRF-Token": csrf},
                json={
                    "runtime_id": "codex-e2e",
                    "display_name": "MCP Docker Agent",
                    "working_directory": "C:\\arena-e2e",
                },
            )
            binding_response.raise_for_status()
            binding = binding_response.json()
            registration = binding["arenaRegistration"]
            if registration["routeStatus"] != "ready":
                raise AssertionError(f"Connector route is not ready: {registration!r}")

            session_command = client.post(
                f"/api/connectors/bindings/{binding['binding_id']}/commands",
                headers={"X-CSRF-Token": csrf},
                json={
                    "action": "session.start",
                    "payload": {"working_directory": "C:\\arena-e2e"},
                    "idempotency_key": f"mcp-e2e-session-{suffix}",
                },
            )
            session_command.raise_for_status()
            command = _receive_type(websocket, "command")
            session_id = f"mcp-session-{suffix}"
            _send_connector_frame(
                websocket,
                "command.ack",
                "mcp-e2e-session-running",
                {
                    "command_id": command["payload"]["command_id"],
                    "status": "running",
                    "session_id": session_id,
                },
            )
            _receive_type(websocket, "ack")
            _send_connector_frame(
                websocket,
                "command.ack",
                "mcp-e2e-session-succeeded",
                {
                    "command_id": command["payload"]["command_id"],
                    "status": "succeeded",
                    "result": {"session_id": session_id},
                },
            )
            _receive_type(websocket, "ack")

            task = asyncio.run(
                _create_task(
                    owner_id=owner_id,
                    agent_id=registration["agentId"],
                    connector_binding_id=binding["binding_id"],
                )
            )
            wake = _receive_type(websocket, "task.available")
            if wake["payload"]["task_id"] != task.task_id:
                raise AssertionError(f"unexpected task wake: {wake!r}")
            _send_connector_frame(
                websocket,
                "task.available.ack",
                "mcp-e2e-wake-ack",
                {
                    key: wake["payload"][key]
                    for key in (
                        "wake_id",
                        "task_id",
                        "binding_id",
                        "binding_epoch",
                    )
                },
            )
            _receive_type(websocket, "ack")

            token_response = client.post(
                "/api/connectors/mcp/token",
                headers={
                    "Authorization": f"Device {credential['device_token']}",
                },
                json={
                    "deviceId": device_id,
                    "bindingId": binding["binding_id"],
                },
            )
            token_response.raise_for_status()
            token = token_response.json()["access_token"]

            discovery = _mcp_call(client, token, "server/discover")
            listed = _mcp_call(client, token, "tools/list")
            synced = _mcp_call(
                client,
                token,
                "tools/call",
                name="arena_sync_agent_tasks",
                arguments={"limit": 20},
            )["structuredContent"]
            if [item["taskId"] for item in synced["tasks"]] != [task.task_id]:
                raise AssertionError(f"sync did not return the task: {synced!r}")

            claimed = _mcp_call(
                client,
                token,
                "tools/call",
                name="arena_claim_agent_task",
                arguments={"taskId": task.task_id},
            )["structuredContent"]
            if claimed["execution"]["sessionId"] != session_id:
                raise AssertionError(f"claim used the wrong session: {claimed!r}")

            result_id = f"mcp-result-{suffix}"
            submitted = _mcp_call(
                client,
                token,
                "tools/call",
                name="arena_submit_agent_task_result",
                arguments={
                    "result": {
                        "resultId": result_id,
                        "taskId": task.task_id,
                        "schemaVersion": "arena.agent-result.v1",
                        "status": "succeeded",
                        "action": {"action": "pass"},
                    }
                },
            )["structuredContent"]
            status = _mcp_call(
                client,
                token,
                "tools/call",
                name="arena_get_agent_task_status",
                arguments={"taskId": task.task_id},
            )["structuredContent"]
            evidence = asyncio.run(_task_evidence(task.task_id))

    if discovery["supportedVersions"] != [MCP_VERSION]:
        raise AssertionError(f"unexpected MCP discovery: {discovery!r}")
    if len(listed["tools"]) != 5:
        raise AssertionError(f"unexpected MCP tool inventory: {listed!r}")
    if submitted["disposition"] not in {"accepted", "duplicate"}:
        raise AssertionError(f"unexpected submit receipt: {submitted!r}")
    if status["status"] != "completed" or not status["hasAuthoritativeResult"]:
        raise AssertionError(f"result is not authoritative: {status!r}")
    if evidence["status"] != "completed" or evidence["result_count"] != 1:
        raise AssertionError(f"PostgreSQL evidence is incomplete: {evidence!r}")

    print(
        json.dumps(
            {
                "docker_api": "healthy",
                "wss_wake": "received_and_acknowledged",
                "mcp_protocol": MCP_VERSION,
                "tools": len(listed["tools"]),
                "sync_task_count": len(synced["tasks"]),
                "claim_session": session_id,
                "result_disposition": submitted["disposition"],
                "task_status": evidence["status"],
                "result_count": evidence["result_count"],
                "chain_writes": 0,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
