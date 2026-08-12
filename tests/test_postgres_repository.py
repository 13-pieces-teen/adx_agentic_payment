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
        self.fetchval_executions: list[tuple[str, tuple[object, ...]]] = []
        self.fetchrow_executions: list[tuple[str, tuple[object, ...]]] = []
        self.fetch_executions: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self):
        return _Transaction()

    async def execute(self, query: str, *arguments):
        self.executions.append((query, arguments))
        return "OK"

    async def fetchval(self, query: str, *arguments):
        self.fetchval_executions.append((query, arguments))
        return 7

    async def fetchrow(self, query: str, *arguments):
        self.fetchrow_executions.append((query, arguments))
        return None

    async def fetch(self, query: str, *arguments):
        self.fetch_executions.append((query, arguments))
        return []


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
        self.fetchval_executions: list[tuple[str, tuple[object, ...]]] = []
        self.execute_executions: list[tuple[str, tuple[object, ...]]] = []
        self.fetch_executions: list[tuple[str, tuple[object, ...]]] = []
        self.fetchrow_executions: list[tuple[str, tuple[object, ...]]] = []

    def acquire(self):
        return _Acquire(self.connection)

    async def fetchval(self, query: str, *arguments):
        self.fetchval_executions.append((query, arguments))
        return 7

    async def execute(self, query: str, *arguments):
        self.execute_executions.append((query, arguments))
        return "UPDATE 1"

    async def fetch(self, query: str, *arguments):
        self.fetch_executions.append((query, arguments))
        return []

    async def fetchrow(self, query: str, *arguments):
        self.fetchrow_executions.append((query, arguments))
        return None


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
        "agent_task_results": [
            {
                "task_id": "task_1",
                "result_id": "result_1",
                "binding_id": "binding_1",
                "device_id": "device_1",
                "command_id": "command_1",
                "binding_epoch": 1,
                "result_hash": "c" * 64,
                "received_at": timestamp,
                "arena_sink_accepted_at": timestamp,
                "result": {
                    "schemaVersion": "arena.agent-result.v1",
                    "resultId": "result_1",
                    "taskId": "task_1",
                    "status": "succeeded",
                    "action": {"action": "pass"},
                },
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
            "connector_agent_task_results",
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
        "connector_agent_task_results": (7,),
        "connector_events": (5,),
        "connector_audit": (4,),
    }
    for table, positions in timestamp_positions.items():
        assert table in inserts
        assert all(
            isinstance(inserts[table][position], datetime) for position in positions
        )

    assert inserts["connector_devices"][5] is None
    result_upsert = next(
        query
        for query, _ in connection.executions
        if "INSERT INTO connector_agent_task_results" in query
    )
    assert "ON CONFLICT (task_id) DO UPDATE SET" in result_upsert
    assert "arena_sink_accepted_at = COALESCE" in result_upsert
    assert "record = EXCLUDED.record" not in result_upsert
    result_arguments = inserts["connector_agent_task_results"]
    assert isinstance(result_arguments[8], datetime)
    assert "arena_sink_accepted_at" not in str(result_arguments[9])


def test_connection_claim_atomically_increments_the_postgres_fencing_token():
    connection = _Connection()
    pool = _Pool(connection)
    repository = PostgresConnectorRepository("postgresql://unused")
    repository._pool = pool

    fencing_token = asyncio.run(
        repository.claim_device_connection(
            "device-1",
            "gateway-a",
            45,
        )
    )

    assert fencing_token == 7
    query, arguments = connection.fetchval_executions[-1]
    assert "INSERT INTO connector_device_connection_leases" in query
    assert "ON CONFLICT (device_id) DO UPDATE SET" in query
    assert "fencing_token + 1" in query
    assert "RETURNING fencing_token" in query
    assert arguments == ("device-1", "gateway-a", 45)
    recovery_query, recovery_arguments = connection.executions[-1]
    assert "UPDATE connector_commands" in recovery_query
    assert "AND status = 'delivered'" in recovery_query
    assert recovery_arguments == ("device-1",)


def test_connection_owner_check_requires_unexpired_matching_fence():
    connection = _Connection()
    pool = _Pool(connection)
    repository = PostgresConnectorRepository("postgresql://unused")
    repository._pool = pool

    owned = asyncio.run(
        repository.is_device_connection_owner("device-1", "gateway-a", 7)
    )

    assert owned is True
    query, arguments = pool.fetchval_executions[-1]
    assert "SELECT EXISTS" in query
    assert "instance_id = $2" in query
    assert "fencing_token = $3" in query
    assert "lease_expires_at > clock_timestamp()" in query
    assert arguments == ("device-1", "gateway-a", 7)


def test_active_connection_check_is_device_scoped_and_requires_unexpired_lease():
    connection = _Connection()
    pool = _Pool(connection)
    repository = PostgresConnectorRepository("postgresql://unused")
    repository._pool = pool

    active = asyncio.run(repository.has_active_device_connection("device-1"))

    assert active is True
    query, arguments = pool.fetchval_executions[-1]
    assert "SELECT EXISTS" in query
    assert "device_id = $1" in query
    assert "lease_expires_at > clock_timestamp()" in query
    assert arguments == ("device-1",)


def test_connection_renew_and_release_are_owner_and_fence_scoped():
    connection = _Connection()
    pool = _Pool(connection)
    repository = PostgresConnectorRepository("postgresql://unused")
    repository._pool = pool

    renewed = asyncio.run(
        repository.renew_device_connection("device-1", "gateway-a", 7, 45)
    )
    released = asyncio.run(
        repository.release_device_connection("device-1", "gateway-a", 7)
    )

    assert renewed is True
    assert released is True
    renew_query, renew_arguments = pool.execute_executions[-2]
    release_query, release_arguments = pool.execute_executions[-1]
    for query in (renew_query, release_query):
        assert "instance_id = $2" in query
        assert "fencing_token = $3" in query
    assert "lease_expires_at > clock_timestamp()" in renew_query
    assert "pg_catalog.make_interval(secs => $4)" in renew_query
    assert renew_arguments == ("device-1", "gateway-a", 7, 45)
    assert "lease_expires_at = clock_timestamp()" in release_query
    assert release_arguments == ("device-1", "gateway-a", 7)


def test_connection_release_and_offline_projection_share_one_fenced_statement():
    connection = _Connection()
    pool = _Pool(connection)
    repository = PostgresConnectorRepository("postgresql://unused")
    repository._pool = pool
    timestamp = "2026-07-24T02:54:50.973998Z"

    released = asyncio.run(
        repository.release_device_connection_and_save_device(
            "device-1",
            "gateway-a",
            7,
            {
                "device_id": "device-1",
                "owner_id": "owner-1",
                "token_hash": "a" * 64,
                "status": "offline",
                "created_at": timestamp,
                "revoked_at": None,
            },
        )
    )

    assert released is True
    query, arguments = pool.fetchval_executions[-1]
    assert "WITH released AS" in query
    assert "UPDATE connector_device_connection_leases" in query
    assert "AND instance_id = $2" in query
    assert "AND fencing_token = $3" in query
    assert "UPDATE connector_devices AS device" in query
    assert "FROM released" in query
    assert arguments[:3] == ("device-1", "gateway-a", 7)
    assert arguments[5] == "offline"


def test_shared_command_route_query_is_limited_to_the_current_connection_owner():
    connection = _Connection()
    pool = _Pool(connection)
    repository = PostgresConnectorRepository("postgresql://unused")
    repository._pool = pool

    routes = asyncio.run(
        repository.list_queued_command_routes_for_connection_owner(
            "gateway-a",
            100,
        )
    )

    assert routes == []
    query, arguments = pool.fetch_executions[-1]
    assert "JOIN connector_device_connection_leases AS lease" in query
    assert "JOIN connector_devices AS device" in query
    assert "lease.instance_id = $1" in query
    assert "lease.lease_expires_at > clock_timestamp()" in query
    assert "device.revoked_at IS NULL" in query
    assert "command.status = 'queued'" in query
    assert "command.expires_at > clock_timestamp()" in query
    assert "binding.record AS binding_record" in query
    assert arguments == ("gateway-a", 100)


def test_delivery_commit_is_conditioned_on_the_live_connection_fence():
    connection = _Connection()
    pool = _Pool(connection)
    repository = PostgresConnectorRepository("postgresql://unused")
    repository._pool = pool

    committed = asyncio.run(
        repository.save_command_for_connection_owner(
            "device-1",
            "gateway-a",
            7,
            {
                "command_id": "command-1",
                "device_id": "device-1",
                "status": "delivered",
            },
        )
    )

    assert committed is True
    query, arguments = pool.fetchval_executions[-1]
    assert "UPDATE connector_commands AS command_row" in query
    assert "command_row.status = 'queued'" in query
    assert "lease.instance_id = $2" in query
    assert "lease.fencing_token = $3" in query
    assert "lease.lease_expires_at > clock_timestamp()" in query
    assert arguments[:4] == ("device-1", "gateway-a", 7, "delivered")


def test_outbound_sequence_save_is_conditioned_on_the_live_fence():
    connection = _Connection()
    pool = _Pool(connection)
    repository = PostgresConnectorRepository("postgresql://unused")
    repository._pool = pool
    committed = asyncio.run(
        repository.save_outbound_sequence_for_connection_owner(
            "device-1",
            "gateway-a",
            7,
            3,
        )
    )

    assert committed is True
    query, arguments = pool.fetchval_executions[-1]
    assert "UPDATE connector_devices AS device_row" in query
    assert "lease.instance_id = $2" in query
    assert "lease.fencing_token = $3" in query
    assert "lease.lease_expires_at > clock_timestamp()" in query
    assert "'{outbound_sequence}'" in query
    assert arguments == ("device-1", "gateway-a", 7, 3)
