from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from arena_agent_contracts import ArenaAgentTaskV1
from connector_gateway.arena_adapter import (
    ConnectorArenaRoute,
    ConnectorArenaRuntimeAdapter,
)
from connector_gateway.service import ConnectorError, ConnectorGateway


def _task() -> ArenaAgentTaskV1:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    return ArenaAgentTaskV1.model_validate(
        {
            "taskId": "task-connector-adapter-1",
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
                "deadlineAt": deadline.isoformat(),
            },
        }
    )


def _gateway() -> ConnectorGateway:
    gateway = ConnectorGateway()
    now = datetime.now(timezone.utc).isoformat()
    gateway.devices["device-1"] = {
        "device_id": "device-1",
        "owner_id": "owner-1",
        "revoked_at": None,
        "binding_epoch": 4,
        "_connection_generation": 0,
        "outbound_sequence": 0,
        "runtimes": [
            {
                "runtime_id": "codex-default",
                "kind": "codex",
                "available": True,
                "capabilities": ["task.dispatch"],
                "task_enabled": True,
                "authentication_status": "configured",
                "arena_compatible": True,
                "arena_isolation": "read_only_ephemeral_schema",
                "local_execution_ready": True,
            }
        ],
    }
    gateway.bindings["binding-1"] = {
        "binding_id": "binding-1",
        "device_id": "device-1",
        "runtime_id": "codex-default",
        "agent_id": "agent-1",
        "binding_epoch": 4,
        "created_at": now,
        "last_session_id": "session-arena-1",
    }
    return gateway


def test_connector_runtime_adapter_dispatches_frozen_typed_task():
    async def scenario():
        gateway = _gateway()
        adapter = ConnectorArenaRuntimeAdapter(gateway)
        task = _task()
        command = await adapter.dispatch(
            task=task,
            route=ConnectorArenaRoute(
                connector_binding_id="binding-1",
                binding_epoch=4,
            ),
        )

        assert command["action"] == "task.dispatch"
        assert command["binding_epoch"] == 4
        assert command["payload"]["session_id"] == "session-arena-1"
        assert command["payload"]["task"] == task.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        )
        assert command["idempotency_key"] == task.idempotency_key

    asyncio.run(scenario())


def test_connector_runtime_adapter_rejects_stale_frozen_route():
    async def scenario():
        adapter = ConnectorArenaRuntimeAdapter(_gateway())
        with pytest.raises(ConnectorError, match="Stale Connector binding epoch"):
            await adapter.dispatch(
                task=_task(),
                route=ConnectorArenaRoute(
                    connector_binding_id="binding-1",
                    binding_epoch=3,
                ),
            )

    asyncio.run(scenario())
