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
LOCAL_CONNECTOR_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "021_arena_local_connector_runtime.sql"
)
CURRENT_GAME_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "024_arena_current_game_projection.sql"
)
CURRENT_GAME_JOIN_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "027_arena_current_game_join_readiness.sql"
)
UNBOUNDED_GAME_CAPACITY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "025_arena_unbounded_game_capacity.sql"
)
LEGACY_GAME_CAPACITY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "026_arena_drop_legacy_game_capacity_check.sql"
)
QUANTITY_AND_LIMIT_ORDERS_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "028_arena_quantity_and_limit_orders.sql"
)
CURRENT_GAME_UPDATED_AT_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "031_arena_current_game_updated_at.sql"
)
CURRENT_GAME_CAPACITY_100_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "032_arena_current_game_capacity_100.sql"
)
CURRENT_GAME_CAPACITY_RESTORED_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "036_arena_current_game_capacity_100.sql"
)
OFFICIAL_PAYMENT_AUTHORITY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "040_arena_official_payment_authority.sql"
)
LEGACY_CURRENT_GAME_CAPACITY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "041_arena_drop_legacy_current_game_capacity_check.sql"
)
AUTHORITATIVE_ACTION_POLICY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "042_arena_authoritative_action_policy.sql"
)
EMPTY_CURRENT_GAME_REFRESH_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "043_arena_refresh_empty_current_game.sql"
)

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


def test_quantity_and_limit_orders_keeps_applied_bytes_immutable() -> None:
    sql = QUANTITY_AND_LIMIT_ORDERS_SQL_PATH.read_text(encoding="utf-8")

    assert "SET LOCAL ROLE adx_arena_migration;" in sql
    assert "RESET ROLE;" not in sql
    assert sql.index("SET LOCAL ROLE adx_arena_migration;") < sql.rindex(
        "COMMIT;"
    )


def test_current_game_updated_at_migration_supports_pointer_rotation() -> None:
    sql = CURRENT_GAME_UPDATED_AT_SQL_PATH.read_text(encoding="utf-8")

    assert "SET LOCAL ROLE adx_arena_migration;" in sql
    assert "ALTER TABLE arena402.current_game" in sql
    assert "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL" in sql
    assert "DEFAULT clock_timestamp()" in sql
    assert sql.index("RESET ROLE;") < sql.rindex("COMMIT;")


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


def test_local_connector_migration_is_owner_scoped_and_supports_mixed_runs():
    sql = LOCAL_CONNECTOR_SQL_PATH.read_text(encoding="utf-8")

    assert "'local_agents.create'" in sql
    assert "FUNCTION reserve_local_agent_idempotency(" in sql
    assert "FUNCTION complete_local_agent_idempotency(" in sql
    assert "FUNCTION resolve_connector_binding_for_arena(" in sql
    assert "d.owner_id = p_owner_user_id" in sql
    assert "(r.record -> 'capabilities') ? 'session.start'" in sql
    assert "(r.record -> 'capabilities') ? 'task.dispatch'" in sql
    assert "UPDATE arena_runtime_bindings AS route" in sql
    assert "u.disabled_at IS NULL" in sql
    assert "v_active_count >= 256" in sql
    assert "runtime_kind IN ('hosted', 'mixed')" in sql
    assert "SET search_path = pg_catalog, public" in sql
    assert (
        "GRANT EXECUTE ON FUNCTION "
        "resolve_connector_binding_for_arena(TEXT, TEXT)"
    ) in sql
    function_owner_grants = sql.split(
        "ALTER FUNCTION resolve_connector_binding_for_arena", maxsplit=1
    )[1]
    assert "GRANT INSERT ON" not in function_owner_grants


def test_local_connector_migration_is_selected_by_arena_scope(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )

    assert LOCAL_CONNECTOR_SQL_PATH in migrate_module.migration_files(
        "arena"
    )
    assert CURRENT_GAME_JOIN_SQL_PATH in migrate_module.migration_files(
        "arena"
    )
    assert CURRENT_GAME_CAPACITY_RESTORED_SQL_PATH in migrate_module.migration_files(
        "arena"
    )


def test_current_game_migration_has_single_pointer_and_product_limits():
    sql = CURRENT_GAME_SQL_PATH.read_text(encoding="utf-8")

    assert "CREATE TABLE arena402.current_game" in sql
    assert "singleton BOOLEAN PRIMARY KEY" in sql
    assert "CHECK (singleton)" in sql
    assert "UNIQUE" in sql
    assert "start_threshold BETWEEN 2 AND 12" in sql
    assert "max_participants BETWEEN start_threshold AND 12" in sql
    assert "REFERENCES arena402.games(game_id)" in sql
    assert "GRANT SELECT ON arena402.current_game TO adx_arena_api" in sql


def test_current_game_capacity_migration_raises_product_limit_to_100():
    sql = CURRENT_GAME_CAPACITY_100_SQL_PATH.read_text(encoding="utf-8")

    assert "current_game_start_threshold_check" in sql
    assert "start_threshold BETWEEN 2 AND 100" in sql
    assert "current_game_max_participants_check" in sql
    assert "max_participants BETWEEN start_threshold AND 100" in sql


def test_latest_current_game_capacity_migration_restores_product_limit_to_100():
    sql = CURRENT_GAME_CAPACITY_RESTORED_SQL_PATH.read_text(encoding="utf-8")

    assert "current_game_start_threshold_check" in sql
    assert "start_threshold BETWEEN 2 AND 100" in sql
    assert "current_game_max_participants_check" in sql
    assert "max_participants BETWEEN start_threshold AND 100" in sql


def test_current_game_join_migration_freezes_readiness_and_dynamic_payees():
    sql = CURRENT_GAME_JOIN_SQL_PATH.read_text(encoding="utf-8")

    assert "CREATE TABLE arena402.join_authorizations" in sql
    assert "join_authorizations_active_user_game_uidx" in sql
    assert "same_game_settlement_account" in sql
    assert "cardinality(allowed_payees) = 0" in sql
    assert "ADD COLUMN payment_mandate_id" in sql
    assert "readiness = 'pending'" in sql
    assert "readiness = 'ready'" in sql
    assert "readiness = 'withdrawn'" in sql
    assert "payment_mandate_id IS NOT NULL" in sql
    assert "portfolio_locked_at IS NOT NULL" in sql
    assert "GRANT SELECT, INSERT, UPDATE ON" in sql


def test_official_payment_authority_keeps_user_and_platform_wallets_distinct():
    sql = OFFICIAL_PAYMENT_AUTHORITY_SQL_PATH.read_text(encoding="utf-8")

    assert "CREATE TABLE arena402.payment_wallet_authorities" in sql
    assert "authority_kind IN ('user', 'platform_official')" in sql
    assert "payment_mandates_wallet_authority_fkey" in sql
    assert "REFERENCES arena402.payment_wallet_authorities" in sql
    assert "sync_user_payment_wallet_authority" in sql
    assert "sync_official_payment_wallet_authority" in sql
    assert "github_subject" not in sql
    assert "private_key" not in sql


def test_official_payment_authority_is_selected_by_arena_scope(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )

    assert OFFICIAL_PAYMENT_AUTHORITY_SQL_PATH in migrate_module.migration_files(
        "arena"
    )


def test_latest_current_game_capacity_cleanup_drops_the_legacy_alias(
    monkeypatch: pytest.MonkeyPatch,
):
    sql = LEGACY_CURRENT_GAME_CAPACITY_SQL_PATH.read_text(encoding="utf-8")

    assert "SET LOCAL ROLE adx_arena_migration;" in sql
    assert "DROP CONSTRAINT IF EXISTS current_game_check" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        LEGACY_CURRENT_GAME_CAPACITY_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_unbounded_game_capacity_keeps_the_per_game_limit_authoritative():
    sql = UNBOUNDED_GAME_CAPACITY_SQL_PATH.read_text(encoding="utf-8")

    assert "DROP CONSTRAINT IF EXISTS games_max_participants_check" in sql
    assert "CHECK (max_participants >= min_participants)" in sql
    assert "arena402.current_game" in sql


def test_unbounded_game_capacity_drops_the_legacy_inline_check():
    sql = LEGACY_GAME_CAPACITY_SQL_PATH.read_text(encoding="utf-8")

    assert "DROP CONSTRAINT IF EXISTS games_check" in sql
    assert "DROP CONSTRAINT IF EXISTS games_max_participants_check" in sql
    assert sql.count("CHECK (max_participants >= min_participants)") == 1


def test_authoritative_action_policy_migration_converges_sql_with_python():
    sql = AUTHORITATIVE_ACTION_POLICY_SQL_PATH.read_text(encoding="utf-8")

    assert "CREATE OR REPLACE FUNCTION apply_arena_agent_task_result" in sql
    assert "buyer_opening_proposal_required" in sql
    assert "counterparty_proposal_required" in sql
    assert "final_turn_must_close" in sql
    assert "OWNER TO adx_arena_migration" in sql
    assert "OWNER TO adx_arena_function_owner" in sql
    assert "insufficient_inventory" in sql


def test_empty_current_game_refresh_never_cancels_a_joined_game():
    sql = EMPTY_CURRENT_GAME_REFRESH_SQL_PATH.read_text(encoding="utf-8")

    assert "UPDATE arena402.games AS game" in sql
    assert "game.phase = 'registration'" in sql
    assert "NOT EXISTS" in sql
    assert "FROM arena402.game_participants AS participant" in sql
    assert "participant.game_id = game.game_id" in sql
