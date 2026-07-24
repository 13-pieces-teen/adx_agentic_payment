"""Migration selection, ordering, checksum, and Arena SQL safety contracts."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MIGRATE_PATH = ROOT / "deploy" / "scripts" / "migrate.py"
ARENA_SQL_PATH = ROOT / "db" / "migrations" / "003_arena_agent_runtime.sql"

_SPEC = importlib.util.spec_from_file_location("arena_migrate", MIGRATE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
if importlib.util.find_spec("asyncpg") is None:
    sys.modules["asyncpg"] = types.SimpleNamespace(
        Connection=object,
        PostgresError=Exception,
        connect=None,
    )
migrate_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migrate_module)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(self, applied: dict[str, str] | None = None) -> None:
        self.applied = dict(applied or {})
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False

    def transaction(self):
        return _Transaction()

    async def fetchval(self, query: str, migration_name: str):
        self.executions.append((query, (migration_name,)))
        return self.applied.get(migration_name)

    async def execute(self, query: str, *arguments):
        self.executions.append((query, arguments))
        if "INSERT INTO adx_schema_migrations" in query:
            migration_name, checksum = arguments
            self.applied[str(migration_name)] = str(checksum)
        return "OK"

    async def close(self):
        self.closed = True


def _write_migration(root: Path, name: str, body: str | None = None) -> Path:
    path = root / name
    path.write_text(body or f"BEGIN;\nSELECT '{name}';\nCOMMIT;\n", encoding="utf-8")
    return path


def test_migration_scopes_exclude_legacy_and_sort_by_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    legacy = _write_migration(tmp_path, "001_initial_schema.sql")
    connector = _write_migration(tmp_path, "002_connector_gateway.sql")
    arena = _write_migration(tmp_path, "003_arena_agent_runtime.sql")
    hosted_api = _write_migration(tmp_path, "004_hosted_agent_api.sql")
    later_arena = _write_migration(tmp_path, "010_arena_more.sql")
    production_runtime = _write_migration(
        tmp_path,
        "005_hosted_agent_production_runtime.sql",
    )
    _write_migration(tmp_path, "004_unapproved.sql")
    monkeypatch.setenv("ADX_CONNECTOR_MIGRATIONS_DIR", str(tmp_path))

    assert migrate_module.migration_files("connector") == [connector]
    assert migrate_module.migration_files("arena") == [
        arena,
        hosted_api,
        production_runtime,
        later_arena,
    ]
    assert migrate_module.migration_files("all") == [
        connector,
        arena,
        hosted_api,
        production_runtime,
        later_arena,
    ]
    assert legacy not in migrate_module.migration_files("all")


def test_migration_scope_rejects_unknown_or_empty_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ADX_CONNECTOR_MIGRATIONS_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="Unsupported migration scope"):
        migrate_module.migration_files("legacy")
    with pytest.raises(RuntimeError, match="No arena migrations"):
        migrate_module.migration_files("arena")


def test_cli_defaults_to_connector_and_accepts_explicit_or_env_scope(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ADX_MIGRATION_SCOPE", raising=False)
    assert migrate_module.parse_args([]).scope == "connector"
    assert migrate_module.parse_args(["--scope", "all"]).scope == "all"

    monkeypatch.setenv("ADX_MIGRATION_SCOPE", "arena")
    assert migrate_module.parse_args([]).scope == "arena"


def test_all_scope_uses_one_global_lock_and_applies_connector_before_arena(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    connector = _write_migration(tmp_path, "002_connector_gateway.sql")
    arena = _write_migration(tmp_path, "003_arena_agent_runtime.sql")
    hosted_api = _write_migration(tmp_path, "004_hosted_agent_api.sql")
    _write_migration(tmp_path, "001_initial_schema.sql")
    connection = _Connection()

    monkeypatch.setenv("ADX_CONNECTOR_MIGRATIONS_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")

    async def fake_connect(_dsn: str):
        return connection

    monkeypatch.setattr(migrate_module, "connect_with_retry", fake_connect)
    asyncio.run(migrate_module.migrate("all"))

    lock_calls = [
        call
        for call in connection.executions
        if "pg_advisory_lock" in call[0]
    ]
    unlock_calls = [
        call
        for call in connection.executions
        if "pg_advisory_unlock" in call[0]
    ]
    assert lock_calls == [
        (
            "SELECT pg_advisory_lock(hashtext($1))",
            (migrate_module.LOCK_NAME,),
        )
    ]
    assert unlock_calls == [
        (
            "SELECT pg_advisory_unlock(hashtext($1))",
            (migrate_module.LOCK_NAME,),
        )
    ]
    assert migrate_module.LOCK_NAME == "adx_schema_migrations"
    assert list(connection.applied) == [
        connector.name,
        arena.name,
        hosted_api.name,
    ]
    assert connection.closed is True

    applied_sql = [
        query
        for query, arguments in connection.executions
        if not arguments
        and "CREATE TABLE IF NOT EXISTS adx_schema_migrations" not in query
        and "pg_advisory_" not in query
    ]
    assert applied_sql == [
        f"SELECT '{connector.name}';",
        f"SELECT '{arena.name}';",
        f"SELECT '{hosted_api.name}';",
    ]


def test_repeated_migration_is_skipped_and_checksum_drift_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    migration = _write_migration(tmp_path, "002_connector_gateway.sql")
    checksum = hashlib.sha256(
        migration.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    monkeypatch.setenv("ADX_CONNECTOR_MIGRATIONS_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")

    already_applied = _Connection({migration.name: checksum})

    async def connect_applied(_dsn: str):
        return already_applied

    monkeypatch.setattr(migrate_module, "connect_with_retry", connect_applied)
    asyncio.run(migrate_module.migrate("connector"))
    assert list(already_applied.applied) == [migration.name]
    assert already_applied.closed is True

    drifted = _Connection({migration.name: "0" * 64})

    async def connect_drifted(_dsn: str):
        return drifted

    monkeypatch.setattr(migrate_module, "connect_with_retry", connect_drifted)
    with pytest.raises(RuntimeError, match="Applied migration changed"):
        asyncio.run(migrate_module.migrate("connector"))
    assert any("pg_advisory_unlock" in query for query, _ in drifted.executions)
    assert drifted.closed is True


def test_arena_sql_has_required_identity_task_result_and_queue_constraints():
    sql = ARENA_SQL_PATH.read_text(encoding="utf-8")

    required_relations = (
        "CREATE TABLE games",
        "CREATE TABLE rounds",
        "CREATE TABLE arena_agents",
        "CREATE TABLE arena_model_credentials",
        "CREATE TABLE arena_hosted_configs",
        "CREATE TABLE arena_runtime_bindings",
        "CREATE TABLE game_agents",
        "CREATE TABLE arena_agent_tasks",
        "CREATE TABLE arena_agent_task_results",
        "CREATE TABLE arena_applied_agent_actions",
        "CREATE TABLE arena_agent_task_attempts",
        "CREATE TABLE arena_agent_task_events",
        "CREATE TABLE hosted_credential_validation_jobs",
        "CREATE TABLE hosted_credential_lifecycle_jobs",
    )
    assert all(relation in sql for relation in required_relations)
    assert "UNIQUE (game_id, user_id)" in sql
    assert "UNIQUE (game_id, agent_id)" in sql
    assert "WHERE disabled_at IS NULL" in sql
    assert "attempt_no BETWEEN 1 AND 2" in sql
    assert "task_id TEXT NOT NULL UNIQUE" in sql
    assert "NUMERIC(78, 0)" in sql
    assert "default_result_id ~ '^default:[0-9a-f]{64}$'" in sql
    assert "runtime_result_id_digest TEXT UNIQUE" in sql
    assert "result_hash ~ '^sha256:[0-9a-f]{64}$'" in sql
    assert "'duplicate_result_ignored'" in sql
    assert "'result_conflict'" in sql
    assert "'late_result_ignored'" in sql
    assert " REAL" not in sql.upper()
    assert "DOUBLE PRECISION" not in sql.upper()
    assert "001_initial_schema" in sql


def test_arena_sql_uses_cas_functions_and_keeps_worker_least_privileged():
    sql = ARENA_SQL_PATH.read_text(encoding="utf-8")

    required_functions = (
        "submit_agent_task_result",
        "submit_hosted_agent_task_result",
        "finalize_expired_agent_task",
        "apply_arena_agent_task_result",
        "reject_arena_agent_task_result",
        "claim_credential_validation_jobs",
        "record_credential_validation_attempt",
        "complete_credential_validation",
        "claim_credential_lifecycle_jobs",
        "complete_credential_lifecycle_job",
    )
    assert all(f"FUNCTION {name}" in sql for name in required_functions)
    assert sql.count("\nSECURITY DEFINER") == sql.count(
        "SET search_path = pg_catalog, public"
    )
    assert "FOR UPDATE OF t SKIP LOCKED" in sql
    assert "result_received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()" in sql
    assert "v_received_at TIMESTAMPTZ := clock_timestamp()" in sql
    assert "pg_catalog.pg_advisory_xact_lock(" in sql
    assert "authoritative_entered_at" in sql
    assert "runtime_update_job_id = p_validation_job_id" in sql
    assert "ga.status IN ('joined', 'active', 'settling')" in sql

    worker_grants = sql.split(
        "-- Hosted Worker can see only frozen execution views", maxsplit=1
    )[1].split("-- Credential Controller", maxsplit=1)[0]
    assert "GRANT INSERT" not in worker_grants
    assert "GRANT UPDATE" not in worker_grants
    assert "arena_applied_agent_actions" not in worker_grants
    assert "GRANT EXECUTE ON FUNCTION submit_agent_task_result(" not in worker_grants
    assert "GRANT EXECUTE ON FUNCTION submit_hosted_agent_task_result(" in worker_grants
    assert "GRANT EXECUTE" in worker_grants
