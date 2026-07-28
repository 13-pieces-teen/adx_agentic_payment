from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from arena_agent_contracts import ArenaAgentTaskV1
from arena_core import ConnectorTaskClaim
from connector_gateway.arena_dispatcher import ConnectorArenaTaskDispatcher
from connector_gateway.service import ConnectorGateway


def _task() -> ArenaAgentTaskV1:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    return ArenaAgentTaskV1.model_validate(
        {
            "taskId": "task-local-dispatch-1",
            "kind": "arena.decide",
            "schemaVersion": "arena.agent-task.v1",
            "gameId": "game-1",
            "roundId": "round-1",
            "gameAgentId": "game-agent-local-1",
            "negotiationId": None,
            "deadlineAt": deadline.isoformat(),
            "idempotencyKey": "game-1:round-1:game-agent-local-1:decide",
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


class _Claims:
    def __init__(self, claim: ConnectorTaskClaim) -> None:
        self.claim = claim
        self.deferred: list[tuple[str, str]] = []

    async def claim_connector_tasks(self, **_: object):
        return (self.claim,)

    async def defer_connector_task(
        self,
        *,
        task_id: str,
        worker_id: str,
        delay_seconds: int,
    ) -> None:
        assert delay_seconds > 0
        self.deferred.append((task_id, worker_id))


def _gateway() -> ConnectorGateway:
    gateway = ConnectorGateway()
    now = datetime.now(timezone.utc).isoformat()
    gateway.devices["device-1"] = {
        "device_id": "device-1",
        "owner_id": "user-1",
        "revoked_at": None,
        "binding_epoch": 5,
        "_connection_generation": 0,
        "outbound_sequence": 0,
        "runtimes": [
            {
                "runtime_id": "codex",
                "kind": "codex",
                "available": True,
                "capabilities": ["session.start", "task.dispatch"],
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
        "runtime_id": "codex",
        "agent_id": "agent-local-1",
        "binding_epoch": 5,
        "working_directory": "E:\\arena",
        "last_session_id": None,
        "created_at": now,
    }
    return gateway


def test_dispatcher_establishes_session_before_dispatching_typed_task() -> None:
    async def scenario() -> None:
        task = _task()
        claim = ConnectorTaskClaim(
            task=task,
            connector_binding_id="binding-1",
            connector_binding_epoch=5,
        )
        claims = _Claims(claim)
        gateway = _gateway()
        dispatcher = ConnectorArenaTaskDispatcher(
            repository=claims,  # type: ignore[arg-type]
            gateway=gateway,
            worker_id="connector-dispatcher-test",
        )

        assert await dispatcher.run_once() == 1
        commands = list(gateway.commands.values())
        assert len(commands) == 1
        assert commands[0]["action"] == "session.start"
        assert commands[0]["payload"]["working_directory"] == "E:\\arena"
        assert claims.deferred == [
            ("task-local-dispatch-1", "connector-dispatcher-test")
        ]

        gateway.bindings["binding-1"]["last_session_id"] = "session-1"
        assert await dispatcher.run_once() == 1
        commands = list(gateway.commands.values())
        assert [value["action"] for value in commands] == [
            "session.start",
            "task.dispatch",
        ]
        assert commands[1]["payload"]["task"]["taskId"] == task.task_id

    asyncio.run(scenario())


def test_dispatcher_recreates_session_after_connector_process_restart() -> None:
    async def scenario() -> None:
        task = _task()
        claim = ConnectorTaskClaim(
            task=task,
            connector_binding_id="binding-1",
            connector_binding_epoch=5,
        )
        claims = _Claims(claim)
        gateway = _gateway()
        gateway.devices["device-1"]["connector_started_at"] = "2026-07-28T10:00:00Z"
        dispatcher = ConnectorArenaTaskDispatcher(
            repository=claims,  # type: ignore[arg-type]
            gateway=gateway,
            worker_id="connector-dispatcher-restart-test",
        )

        assert await dispatcher.run_once() == 1
        first_start = next(iter(gateway.commands.values()))
        await gateway.acknowledge_command(
            "device-1",
            {
                "command_id": first_start["command_id"],
                "status": "succeeded",
                "result": {"session_id": "session-before-restart"},
            },
        )

        await gateway.apply_hello(
            "device-1",
            {
                "protocol_version": "1.0",
                "connector_version": "0.1.0",
                "started_at": "2026-07-28T10:05:00Z",
            },
        )
        assert gateway.bindings["binding-1"]["last_session_id"] is None

        assert await dispatcher.run_once() == 1
        session_starts = [
            command
            for command in gateway.commands.values()
            if command["action"] == "session.start"
        ]
        assert len(session_starts) == 2
        restarted_session = next(
            command
            for command in session_starts
            if command["command_id"] != first_start["command_id"]
        )
        await gateway.acknowledge_command(
            "device-1",
            {
                "command_id": restarted_session["command_id"],
                "status": "succeeded",
                "result": {"session_id": "session-after-restart"},
            },
        )

        assert await dispatcher.run_once() == 1
        task_dispatch = next(
            command
            for command in gateway.commands.values()
            if command["action"] == "task.dispatch"
        )
        assert task_dispatch["payload"]["session_id"] == "session-after-restart"
        assert task_dispatch["payload"]["task"]["taskId"] == task.task_id

    asyncio.run(scenario())


def test_dispatcher_retries_inflight_task_once_after_connector_restart() -> None:
    async def scenario() -> None:
        task = _task()
        claim = ConnectorTaskClaim(
            task=task,
            connector_binding_id="binding-1",
            connector_binding_epoch=5,
        )
        claims = _Claims(claim)
        gateway = _gateway()
        gateway.devices["device-1"]["connector_started_at"] = "2026-07-28T10:00:00Z"
        dispatcher = ConnectorArenaTaskDispatcher(
            repository=claims,  # type: ignore[arg-type]
            gateway=gateway,
            worker_id="connector-dispatcher-inflight-restart-test",
        )

        await dispatcher.run_once()
        first_start = next(iter(gateway.commands.values()))
        await gateway.acknowledge_command(
            "device-1",
            {
                "command_id": first_start["command_id"],
                "status": "succeeded",
                "result": {"session_id": "session-before-restart"},
            },
        )
        await dispatcher.run_once()
        first_dispatch = next(
            command
            for command in gateway.commands.values()
            if command["action"] == "task.dispatch"
        )
        await gateway.acknowledge_command(
            "device-1",
            {
                "command_id": first_dispatch["command_id"],
                "status": "accepted",
                "result": {"task_id": task.task_id},
            },
        )

        await gateway.apply_hello(
            "device-1",
            {
                "protocol_version": "1.0",
                "connector_version": "0.1.0",
                "started_at": "2026-07-28T10:05:00Z",
            },
        )
        await gateway.acknowledge_command(
            "device-1",
            {
                "command_id": first_dispatch["command_id"],
                "status": "failed",
                "result": {"task_id": task.task_id},
                "error": {
                    "code": "connector_restarted",
                    "message": "Connector restarted before task completion",
                },
            },
        )

        await dispatcher.run_once()
        restarted_session = max(
            (
                command
                for command in gateway.commands.values()
                if command["action"] == "session.start"
            ),
            key=lambda command: command["created_at"],
        )
        await gateway.acknowledge_command(
            "device-1",
            {
                "command_id": restarted_session["command_id"],
                "status": "succeeded",
                "result": {"session_id": "session-after-restart"},
            },
        )

        await dispatcher.run_once()
        task_dispatches = [
            command
            for command in gateway.commands.values()
            if command["action"] == "task.dispatch"
        ]
        assert len(task_dispatches) == 2
        retry = next(
            command
            for command in task_dispatches
            if command["command_id"] != first_dispatch["command_id"]
        )
        assert retry["idempotency_key"] == task.idempotency_key
        assert retry["payload"]["session_id"] == "session-after-restart"
        assert retry["payload"]["task"]["taskId"] == task.task_id

    asyncio.run(scenario())
