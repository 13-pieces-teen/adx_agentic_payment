from __future__ import annotations

import asyncio
from datetime import datetime

from connector_gateway.postgres_repository import PostgresConnectorRepository


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self):
        return _Transaction()

    async def execute(self, query: str, *arguments):
        self.executions.append((query, arguments))
        return "OK"


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


def test_gateway_iso_timestamps_are_bound_as_datetimes_for_asyncpg():
    connection = _Connection()
    repository = PostgresConnectorRepository("postgresql://unused")
    repository._pool = _Pool(connection)
    timestamp = "2026-07-24T02:54:50.973998Z"
    state = {
        "pairings": [
            {
                "pairing_id": "pair_1",
                "user_code": "ABCD-EFGH",
                "owner_id": None,
                "device_code_hash": "a" * 64,
                "status": "pending",
                "created_at": timestamp,
                "expires_at": timestamp,
            }
        ],
        "devices": [
            {
                "device_id": "device_1",
                "owner_id": "owner_1",
                "token_hash": "b" * 64,
                "status": "offline",
                "created_at": timestamp,
                "revoked_at": None,
                "runtimes": [],
            }
        ],
        "bindings": [
            {
                "binding_id": "binding_1",
                "device_id": "device_1",
                "runtime_id": "codex",
                "agent_id": "agent_1",
                "status": "active",
                "created_at": timestamp,
            }
        ],
        "commands": [
            {
                "command_id": "command_1",
                "binding_id": "binding_1",
                "device_id": "device_1",
                "status": "queued",
                "action": "runtime.probe",
                "idempotency_key": "probe-1",
                "created_at": timestamp,
                "expires_at": timestamp,
            }
        ],
        "events": [
            {
                "event_id": "event_1",
                "device_id": "device_1",
                "binding_id": "binding_1",
                "sequence": 1,
                "event_type": "runtime.status",
                "received_at": timestamp,
            }
        ],
        "audit": [
            {
                "audit_id": "audit_1",
                "owner_id": None,
                "action": "pairing.created",
                "actor": "connector",
                "occurred_at": timestamp,
            }
        ],
    }

    asyncio.run(repository.save_gateway_state(state))

    inserts = {
        table: arguments
        for query, arguments in connection.executions
        for table in (
            "connector_pairings",
            "connector_devices",
            "connector_bindings",
            "connector_commands",
            "connector_events",
            "connector_audit",
        )
        if f"INSERT INTO {table}" in query
    }
    timestamp_positions = {
        "connector_pairings": (5, 6),
        "connector_devices": (4,),
        "connector_bindings": (5,),
        "connector_commands": (6, 7),
        "connector_events": (5,),
        "connector_audit": (4,),
    }
    for table, positions in timestamp_positions.items():
        assert table in inserts
        assert all(
            isinstance(inserts[table][position], datetime) for position in positions
        )

    assert inserts["connector_devices"][5] is None
