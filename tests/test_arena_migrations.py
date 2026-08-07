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
SETTLEMENT_FENCING_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "044_arena_settlement_audit_and_facilitator_fencing.sql"
)
PLATFORM_ACCOUNT_WALLETS_SQL_PATH = (
    ROOT / "db" / "migrations" / "045_arena_platform_account_wallets.sql"
)
NEGOTIATION_SETTLEMENT_STATUS_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "046_arena_negotiation_settlement_status.sql"
)
SEMANTIC_CANDIDATE_POLICY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "047_arena_semantic_candidate_policy.sql"
)
BARGAINING_OPENING_POLICY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "048_arena_bargaining_opening_offer.sql"
)
BATCHED_FINALIZER_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "049_arena_batch_deadline_finalizer.sql"
)
PROVIDER_CAPACITY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "050_arena_hosted_provider_capacity_and_fair_claim.sql"
)
RUNTIME_RUN_FENCING_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "051_arena_runtime_run_lease_fencing.sql"
)
OFFICIAL_LITELLM_CAPACITY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "052_arena_official_litellm_provider_capacity.sql"
)
SCHEMA_IDENTITY_READINESS_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "053_arena_schema_identity_readiness.sql"
)
FIXED_TRADE_QUANTITY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "054_arena_fixed_trade_quantity.sql"
)
AGENT_DRIVEN_A2A_MARKET_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "055_arena_agent_driven_a2a_market.sql"
)
AGENT_DRIVEN_RUNTIME_TASKS_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "056_arena_agent_driven_runtime_tasks.sql"
)
AGENT_DRIVEN_ROUND_PROTOCOL_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "057_arena_agent_driven_round_protocol.sql"
)
AGENT_MARKET_PROJECTION_PRIVILEGES_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "058_arena_agent_market_projection_privileges.sql"
)
AGENT_NEGOTIATION_AUTONOMY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "059_arena_agent_negotiation_autonomy.sql"
)
BINDING_RFQ_FALLBACK_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "060_arena_binding_rfq_and_sequential_fallback.sql"
)
A2A_ENGAGEMENT_POOL_ENTRIES_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "061_arena_a2a_engagement_pool_entries.sql"
)
AGENT_MARKET_TERMINALIZATION_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "062_arena_agent_market_terminalization.sql"
)
HOSTED_AGENT_RUNTIME_V2_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "063_hosted_agent_runtime_v2.sql"
)
HOSTED_AGENT_CROSS_GAME_LEARNING_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "064_hosted_agent_cross_game_learning.sql"
)
HOSTED_AGENT_MEMORY_CONTEXT_BARRIER_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "065_hosted_agent_memory_context_barrier.sql"
)
HOSTED_AGENT_MEMORY_CANDIDATE_OUTCOME_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "066_hosted_agent_memory_candidate_outcome.sql"
)
HOSTED_AGENT_STRATEGY_FOUNDATION_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "067_hosted_agent_strategy_foundation.sql"
)
ARENA_MONEY_PRECISION_POLICY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "068_arena_money_precision_policy.sql"
)
ARENA_MONEY_PRECISION_RUN_RECOVERY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "069_arena_money_precision_run_recovery.sql"
)
ARENA_ELAPSED_TASK_RUN_RECOVERY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "070_arena_elapsed_task_run_recovery.sql"
)
HOSTED_LARGE_FOUNDATION_LEARNING_RECOVERY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "071_hosted_agent_large_foundation_learning_recovery.sql"
)
ARENA_MONEY_PRECISION_PRIVILEGES_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "072_arena_money_precision_function_privileges.sql"
)
ARENA_PRECISION_PRIVILEGE_RUN_RECOVERY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "073_arena_precision_privilege_run_recovery.sql"
)
ARENA_PHASE_D_EMPTY_CURRENT_GAME_CUTOVER_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "074_arena_phase_d_empty_current_game_cutover.sql"
)
ARENA_MARKET_PROJECTION_RUN_RECOVERY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "075_arena_market_projection_run_recovery.sql"
)
SETTLEMENT_NEGOTIATION_PRIVILEGES_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "076_arena_settlement_negotiation_privileges.sql"
)
SETTLEMENT_ENGAGEMENT_TERMINALIZATION_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "077_arena_settlement_engagement_terminalization.sql"
)
MARKET_RFQ_PROJECTION_RUN_RECOVERY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "078_arena_market_rfq_projection_run_recovery.sql"
)
CONTEXT_LIQUIDITY_RUN_RECOVERY_SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "079_arena_context_liquidity_run_recovery.sql"
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

    async def fetch(self, query: str):
        self.executions.append((query, ()))
        return [
            {"migration_name": name, "sha256": checksum}
            for name, checksum in sorted(self.applied.items())
        ]

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


def test_schema_identity_readiness_grants_registry_read_only() -> None:
    sql = SCHEMA_IDENTITY_READINESS_SQL_PATH.read_text(encoding="utf-8")

    assert "GRANT SELECT ON TABLE public.adx_schema_migrations TO" in sql
    for role in (
        "adx_connector_gateway",
        "adx_arena_api",
        "adx_arena_core",
        "adx_hosted_worker",
        "adx_credential_controller",
        "adx_settlement",
        "adx_wallet_signer",
    ):
        assert role in sql
    assert "INSERT" not in sql
    assert "UPDATE" not in sql
    assert "DELETE" not in sql


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
        and "FROM adx_schema_migrations" not in query
        and "pg_advisory_" not in query
        and query != "RESET ROLE"
    ]
    assert applied_sql == [
        f"SELECT '{connector.name}';",
        f"SELECT '{arena.name}';",
        f"SELECT '{hosted_api.name}';",
    ]
    assert [
        query
        for query, arguments in connection.executions
        if query == "RESET ROLE" and not arguments
    ] == ["RESET ROLE", "RESET ROLE", "RESET ROLE"]


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


def test_all_scope_rejects_all_manifest_drift_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = _write_migration(tmp_path, "002_connector_gateway.sql")
    checksum = hashlib.sha256(
        connector.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    connection = _Connection(
        {
            connector.name: "0" * 64,
            "011_arena_scalable_games.sql": "orphan-sha",
        }
    )
    monkeypatch.setenv("ADX_CONNECTOR_MIGRATIONS_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")

    async def fake_connect(_dsn: str):
        return connection

    monkeypatch.setattr(migrate_module, "connect_with_retry", fake_connect)

    with pytest.raises(RuntimeError) as error:
        asyncio.run(migrate_module.migrate("all"))

    assert (
        "Applied migration missing from disk: "
        "011_arena_scalable_games.sql"
    ) in str(error.value)
    assert (
        "Applied migration changed on disk: "
        "002_connector_gateway.sql"
    ) in str(error.value)
    executed_migration_sql = [
        query
        for query, arguments in connection.executions
        if not arguments
        and query.strip().startswith("SELECT '")
    ]
    assert executed_migration_sql == []
    assert connection.applied == {
        connector.name: "0" * 64,
        "011_arena_scalable_games.sql": "orphan-sha",
    }
    assert connection.closed is True


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


def test_platform_account_wallet_migration_removes_github_as_a_requirement():
    sql = PLATFORM_ACCOUNT_WALLETS_SQL_PATH.read_text(encoding="utf-8")

    assert "SET LOCAL ROLE adx_arena_migration;" in sql
    assert "ALTER COLUMN github_subject DROP NOT NULL" in sql
    assert "user_id is the platform wallet authority" in sql
    assert "private_key" not in sql.lower()


def test_negotiation_settlement_status_migration_adds_terminal_states() -> None:
    sql = NEGOTIATION_SETTLEMENT_STATUS_SQL_PATH.read_text(encoding="utf-8")

    assert "DROP CONSTRAINT IF EXISTS negotiations_status_check" in sql
    assert "'settled'" in sql
    assert "'settlement_failed'" in sql


def test_settlement_role_can_terminalize_a_negotiation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = SETTLEMENT_NEGOTIATION_PRIVILEGES_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "SET LOCAL ROLE adx_arena_migration;" in sql
    assert "GRANT SELECT, UPDATE ON" in sql
    assert "arena402.negotiations" in sql
    assert "TO adx_settlement;" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        SETTLEMENT_NEGOTIATION_PRIVILEGES_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_settlement_engagement_terminalization_is_backfilled_and_authorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = SETTLEMENT_ENGAGEMENT_TERMINALIZATION_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "GRANT SELECT, UPDATE ON" in sql
    assert "arena402.market_engagements" in sql
    assert "TO adx_settlement;" in sql
    assert "UPDATE arena402.market_engagements AS engagement" in sql
    assert "intent.status = 'inventory_committed'" in sql
    assert "'settlement_failed'" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        SETTLEMENT_ENGAGEMENT_TERMINALIZATION_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_semantic_candidate_policy_converges_sql_with_python() -> None:
    sql = SEMANTIC_CANDIDATE_POLICY_SQL_PATH.read_text(encoding="utf-8")

    assert "CREATE OR REPLACE FUNCTION apply_arena_agent_task_result" in sql
    assert "limit_price_violation" in sql
    assert "in_bound_quote_must_accept" in sql
    assert "out_of_bound_quote_must_counter" in sql
    assert "final_out_of_bound_quote_must_reject" in sql
    assert "counter_must_equal_limit" in sql


def test_bargaining_opening_policy_preserves_bounds_without_forcing_ceiling():
    sql = BARGAINING_OPENING_POLICY_SQL_PATH.read_text(encoding="utf-8")

    assert "CREATE OR REPLACE FUNCTION apply_arena_agent_task_result" in sql
    assert "buyer_opening_proposal_required" in sql
    assert "limit_price_violation" in sql
    assert "in_bound_quote_must_accept" in sql
    assert "counter_must_equal_limit" in sql
    assert "opening_price_must_equal_limit" not in sql


def test_platform_account_wallet_migration_is_selected_by_arena_scope(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )

    assert PLATFORM_ACCOUNT_WALLETS_SQL_PATH in migrate_module.migration_files("arena")


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


def test_settlement_fencing_migration_enforces_signed_evidence_and_db_fence():
    sql = SETTLEMENT_FENCING_SQL_PATH.read_text(encoding="utf-8")

    assert "SET LOCAL ROLE adx_arena_migration;" in sql
    assert "x402_attempt_payment_payload_digest_required" in sql
    assert "payment_payload_digest IS NOT NULL" in sql
    assert "NOT VALID" in sql
    assert "CREATE TABLE arena402.facilitator_broadcast_fences" in sql
    assert "facilitator_id TEXT PRIMARY KEY" in sql
    assert "UNIQUE (settlement_intent_id)" in sql
    assert "TO adx_settlement" in sql


def test_empty_current_game_refresh_never_cancels_a_joined_game():
    sql = EMPTY_CURRENT_GAME_REFRESH_SQL_PATH.read_text(encoding="utf-8")

    assert "UPDATE arena402.games AS game" in sql
    assert "game.phase = 'registration'" in sql
    assert "NOT EXISTS" in sql
    assert "FROM arena402.game_participants AS participant" in sql
    assert "participant.game_id = game.game_id" in sql


def test_concurrency_migrations_are_bounded_fair_and_fenced():
    finalizer = BATCHED_FINALIZER_SQL_PATH.read_text(encoding="utf-8")
    capacity = PROVIDER_CAPACITY_SQL_PATH.read_text(encoding="utf-8")
    fencing = RUNTIME_RUN_FENCING_SQL_PATH.read_text(encoding="utf-8")
    official_capacity = OFFICIAL_LITELLM_CAPACITY_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "finalize_expired_agent_tasks_batch" in finalizer
    assert "FOR UPDATE SKIP LOCKED" in finalizer
    assert "p_limit > 1000" in finalizer
    assert "hosted_provider_capacity" in capacity
    assert "pg_advisory_xact_lock" in capacity
    assert "PARTITION BY task.game_id" in capacity
    assert "capacity.max_inflight" in capacity
    assert "ADD COLUMN lease_epoch BIGINT" in fencing
    assert "CHECK (lease_epoch >= 0)" in fencing
    assert "('official-deepseek', 32)" in official_capacity
    assert "ON CONFLICT (provider) DO NOTHING" in official_capacity


def test_concurrency_migrations_are_selected_for_arena_scope(monkeypatch):
    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )

    selected = set(migrate_module.migration_files("arena"))

    assert BATCHED_FINALIZER_SQL_PATH in selected
    assert PROVIDER_CAPACITY_SQL_PATH in selected
    assert RUNTIME_RUN_FENCING_SQL_PATH in selected
    assert OFFICIAL_LITELLM_CAPACITY_SQL_PATH in selected


def test_fixed_trade_quantity_migration_is_forward_only_and_selected(
    monkeypatch,
):
    sql = FIXED_TRADE_QUANTITY_SQL_PATH.read_text(encoding="utf-8")

    assert "arena_agent_task_results_fixed_trade_quantity_check" in sql
    assert "candidate_action ->> 'action' NOT IN ('buy', 'sell')" in sql
    assert "NOT (candidate_action ? 'quantity')" in sql
    assert "candidate_action -> 'quantity' = '1'::JSONB" in sql
    assert "NOT VALID" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert FIXED_TRADE_QUANTITY_SQL_PATH in migrate_module.migration_files(
        "arena"
    )


def test_agent_driven_a2a_market_requires_agent_result_provenance_and_privacy(
    monkeypatch,
):
    sql = AGENT_DRIVEN_A2A_MARKET_SQL_PATH.read_text(encoding="utf-8")

    for relation in (
        "CREATE TABLE arena402.market_result_applications",
        "CREATE TABLE arena402.market_projection_receipts",
        "CREATE TABLE arena402.market_intents",
        "CREATE TABLE arena402.market_negotiation_requests",
        "CREATE TABLE arena402.market_engagements",
        "CREATE TABLE arena402.participant_round_slots",
        "CREATE TABLE arena402.market_deals",
    ):
        assert relation in sql
    assert (
        "result_id TEXT PRIMARY KEY\n"
        "        REFERENCES public.arena_agent_task_results(result_id)"
    ) in sql
    for action_kind in (
        "'intent'",
        "'rfq'",
        "'engage'",
        "'proposal'",
        "'acceptance'",
    ):
        assert f"CHECK (source_action_kind = {action_kind})" in sql or (
            f"CHECK (selection_action_kind = {action_kind})" in sql
            or f"CHECK (latest_proposal_action_kind = {action_kind})" in sql
            or f"CHECK (acceptance_action_kind = {action_kind})" in sql
        )
    assert sql.count(
        "REFERENCES arena402.market_result_applications("
    ) >= 6
    assert "selection_result_id TEXT NOT NULL UNIQUE" in sql
    assert "latest_proposal_result_id TEXT NOT NULL UNIQUE" in sql
    assert "acceptance_result_id TEXT NOT NULL UNIQUE" in sql
    assert "latest_proposal_result_id <> acceptance_result_id" in sql
    assert "quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity = 1)" in sql
    assert "CREATE VIEW arena402.market_directory_public" in sql
    public_view = sql.split(
        "CREATE VIEW arena402.market_directory_public",
        maxsplit=1,
    )[1].split("REVOKE ALL ON TABLE", maxsplit=1)[0]
    assert "limit_price_atomic" not in public_view
    api_grants = sql.split(
        "REVOKE ALL ON TABLE",
        maxsplit=1,
    )[1]
    assert (
        "GRANT SELECT ON TABLE arena402.market_directory_public TO"
        in api_grants
    )
    assert "market_intents TO adx_arena_api" not in api_grants
    intent_table = sql.split(
        "CREATE TABLE arena402.market_intents",
        maxsplit=1,
    )[1].split(
        "CREATE TABLE arena402.market_negotiation_requests",
        maxsplit=1,
    )[0]
    request_table = sql.split(
        "CREATE TABLE arena402.market_negotiation_requests",
        maxsplit=1,
    )[1].split(
        "CREATE TABLE arena402.market_engagements",
        maxsplit=1,
    )[0]
    assert "source_result_id TEXT NOT NULL UNIQUE" in intent_table
    assert "source_result_id TEXT NOT NULL," in request_table
    assert "source_result_id TEXT NOT NULL UNIQUE" not in request_table
    assert "UNIQUE (source_result_id, seller_intent_id)" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert AGENT_DRIVEN_A2A_MARKET_SQL_PATH in migrate_module.migration_files(
        "arena"
    )


def test_agent_driven_runtime_task_kinds_preserve_fcfs_and_fail_closed(
    monkeypatch,
):
    sql = AGENT_DRIVEN_RUNTIME_TASKS_SQL_PATH.read_text(encoding="utf-8")

    assert (
        "RENAME TO apply_arena_agent_task_result_fcfs_v1"
        in sql
    )
    assert (
        "RETURN public.apply_arena_agent_task_result_fcfs_v1(p_result_id)"
        in sql
    )
    for task_kind in (
        "arena.market.intent",
        "arena.market.rfq",
        "arena.market.select",
    ):
        assert task_kind in sql
    for violation in (
        "market_price_required",
        "market_price_boundary_violation",
        "rfq_target_not_visible",
        "request_not_visible",
        "insufficient_inventory",
    ):
        assert violation in sql
    assert "'market_timeout'" in sql
    assert (
        "GRANT EXECUTE ON FUNCTION apply_arena_agent_task_result(TEXT)"
        in sql
    )

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        AGENT_DRIVEN_RUNTIME_TASKS_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_agent_driven_round_protocol_is_opt_in_and_keeps_fcfs_default(
    monkeypatch,
):
    sql = AGENT_DRIVEN_ROUND_PROTOCOL_SQL_PATH.read_text(
        encoding="utf-8"
    )
    assert "DEFAULT 'fcfs.v1'" in sql
    assert "'agent_a2a.v1'" in sql
    assert "games_market_protocol_snapshot_check" in sql
    assert "UPDATE public.games" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        AGENT_DRIVEN_ROUND_PROTOCOL_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_agent_market_projection_receives_read_only_authority(
    monkeypatch,
):
    sql = AGENT_MARKET_PROJECTION_PRIVILEGES_SQL_PATH.read_text(
        encoding="utf-8"
    )
    assert "GRANT SELECT ON TABLE" in sql
    assert "public.arena_agent_tasks" in sql
    assert "public.arena_agent_task_results" in sql
    assert "public.arena_applied_agent_actions" in sql
    assert "TO adx_arena_core" in sql
    assert "GRANT INSERT" not in sql
    assert "GRANT UPDATE" not in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        AGENT_MARKET_PROJECTION_PRIVILEGES_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_agent_negotiation_policy_validates_bounds_without_choosing_strategy(
    monkeypatch,
):
    sql = AGENT_NEGOTIATION_AUTONOMY_SQL_PATH.read_text(
        encoding="utf-8"
    )
    assert (
        "CREATE OR REPLACE FUNCTION "
        "apply_arena_agent_task_result_fcfs_v1" in sql
    )
    assert "buyer_opening_proposal_required" in sql
    assert "counterparty_proposal_required" in sql
    assert "final_turn_must_close" in sql
    assert "limit_price_violation" in sql
    assert "in_bound_quote_must_accept" not in sql
    assert "out_of_bound_quote_must_counter" not in sql
    assert "counter_must_equal_limit" not in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        AGENT_NEGOTIATION_AUTONOMY_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_binding_rfq_and_sequential_fallback_are_durable(
    monkeypatch,
):
    sql = BINDING_RFQ_FALLBACK_SQL_PATH.read_text(encoding="utf-8")

    assert "ADD COLUMN attempt_sequence INTEGER" in sql
    assert "ALTER COLUMN attempt_sequence SET NOT NULL" in sql
    assert "attempt_sequence BETWEEN 1 AND 3" in sql
    assert "market_requests_one_active_buyer_rfq_uidx" in sql
    assert "status IN ('pending', 'engaged')" in sql
    assert "latest_proposal_request_id TEXT" in sql
    assert "latest_proposal_action_kind IN ('rfq', 'proposal')" in sql
    assert "latest_proposal_request_id = request_id" in sql
    assert "CREATE TABLE arena402.market_rfq_sessions" in sql
    assert "frozen_directory JSONB NOT NULL" in sql
    assert "market_engagements_terminal_status_check" in sql
    assert "'timed_out'" in sql
    assert "TO adx_arena_core" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert BINDING_RFQ_FALLBACK_SQL_PATH in migrate_module.migration_files(
        "arena"
    )


def test_a2a_engagement_entries_preserve_fcfs_participant_uniqueness(
    monkeypatch,
):
    sql = A2A_ENGAGEMENT_POOL_ENTRIES_SQL_PATH.read_text(encoding="utf-8")

    assert "ADD COLUMN market_engagement_id TEXT" in sql
    assert "DROP CONSTRAINT pool_entries_round_id_game_participant_id_key" in sql
    assert "pool_entries_fcfs_participant_uidx" in sql
    assert "WHERE market_engagement_id IS NULL" in sql
    assert "pool_entries_a2a_engagement_participant_uidx" in sql
    assert "WHERE market_engagement_id IS NOT NULL" in sql
    assert "REFERENCES arena402.market_engagements(engagement_id)" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        A2A_ENGAGEMENT_POOL_ENTRIES_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_completed_round_market_state_is_backfilled_to_terminal(
    monkeypatch,
):
    sql = AGENT_MARKET_TERMINALIZATION_SQL_PATH.read_text(encoding="utf-8")

    assert "UPDATE arena402.market_rfq_sessions" in sql
    assert "SET status = 'expired'" in sql
    assert "UPDATE arena402.market_negotiation_requests" in sql
    assert "UPDATE arena402.participant_round_slots" in sql
    assert "SET status = 'available'" in sql
    assert "UPDATE arena402.market_intents" in sql
    assert "status IN ('open', 'reserved')" in sql
    assert "expires_at = LEAST" in sql
    assert "round_row.phase IN ('completed', 'cancelled')" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        AGENT_MARKET_TERMINALIZATION_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_hosted_agent_runtime_v2_persists_strategy_and_applied_memory(
    monkeypatch,
):
    sql = HOSTED_AGENT_RUNTIME_V2_SQL_PATH.read_text(encoding="utf-8")

    assert "strategy_archetype" in sql
    assert "hosted_agent_strategy_revisions" in sql
    assert "hosted_agent_game_memory" in sql
    assert "hosted_agent_memory_patches" in sql
    assert "hosted_strategy_revision_id" in sql
    assert "load_hosted_agent_runtime_context" in sql
    assert "stage_hosted_agent_memory_patch" in sql
    assert "project_hosted_agent_memory_patches" in sql
    assert "complete_pydantic_agent_task_attempt" in sql
    assert "agent_request_count" in sql
    assert "agent_tool_call_count" in sql
    assert "patch.runtime_result_id_digest" in sql
    assert "result.runtime_result_id_digest =" in sql
    assert "v_patch.apply_status = 'applied'" in sql
    assert "memory_version = v_patch.expected_memory_version" in sql
    assert "TO adx_hosted_worker" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert HOSTED_AGENT_RUNTIME_V2_SQL_PATH in migrate_module.migration_files(
        "arena"
    )


def test_hosted_agent_cross_game_learning_is_durable_and_gated(
    monkeypatch,
):
    sql = HOSTED_AGENT_CROSS_GAME_LEARNING_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "hosted_agent_learning_jobs" in sql
    assert "hosted_agent_strategy_evaluations" in sql
    assert "arena_game_completed_hosted_learning" in sql
    assert "claim_hosted_agent_learning_jobs" in sql
    assert "load_hosted_agent_learning_evidence" in sql
    assert "complete_hosted_agent_learning_job" in sql
    assert "release_hosted_agent_learning_job" in sql
    assert "status = 'superseded'" in sql
    assert "status = 'rolled_back'" in sql
    assert "automatic_regression_rollback" in sql
    assert "(v_net_worth - v_average_net_worth)" in sql
    assert "/ v_average_net_worth" in sql
    assert "v_average_net_worth <= 0" in sql
    assert "v_base_evaluation_count >= 1" in sql
    assert "v_base_average <= v_parent_average - 2000" in sql
    assert "TO adx_hosted_worker" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        HOSTED_AGENT_CROSS_GAME_LEARNING_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_hosted_agent_context_projects_prior_applied_memory(
    monkeypatch,
):
    sql = HOSTED_AGENT_MEMORY_CONTEXT_BARRIER_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "project_hosted_agent_memory_for_context" in sql
    assert "patch.game_agent_id = p_game_agent_id" in sql
    assert "result.apply_status IN ('applied', 'rejected')" in sql
    assert "memory_version = v_patch.expected_memory_version" in sql
    assert "PERFORM public.project_hosted_agent_memory_for_context" in sql
    assert "load_hosted_agent_runtime_context" in sql
    assert "TO adx_hosted_worker" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        HOSTED_AGENT_MEMORY_CONTEXT_BARRIER_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_hosted_agent_memory_only_learns_applied_candidate_outcomes(
    monkeypatch,
):
    sql = HOSTED_AGENT_MEMORY_CANDIDATE_OUTCOME_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "arena_applied_agent_actions AS application" in sql
    assert "application.application_outcome" in sql
    assert "v_patch.application_outcome = 'candidate'" in sql
    assert "SET status = 'discarded'" in sql
    assert "project_hosted_agent_memory_patches" in sql
    assert "project_hosted_agent_memory_for_context" in sql
    assert "TO adx_hosted_worker" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        HOSTED_AGENT_MEMORY_CANDIDATE_OUTCOME_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_hosted_agent_learning_preserves_strategy_foundation(monkeypatch):
    sql = HOSTED_AGENT_STRATEGY_FOUNDATION_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "load_hosted_agent_learning_evidence_v2" in sql
    assert "WITH RECURSIVE strategy_lineage" in sql
    assert "baseStrategyInstructions" in sql
    assert "arena.hosted-learning-evidence.v2" in sql
    assert "source <> 'learned'" in sql
    assert "TO adx_hosted_worker" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        HOSTED_AGENT_STRATEGY_FOUNDATION_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_arena_money_precision_policy_wraps_apply_and_repairs_unprojected(
    monkeypatch,
):
    sql = ARENA_MONEY_PRECISION_POLICY_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "price_precision_exceeded" in sql
    assert "apply_arena_agent_task_result_pre_precision_v1" in sql
    assert (
        "CREATE OR REPLACE FUNCTION public.apply_arena_agent_task_result("
        in sql
    )
    assert "market_projection_receipts" in sql
    assert "'default_pass'" in sql
    assert "'market_timeout'" in sql
    assert "'negotiation_timeout'" in sql
    assert "GRANT EXECUTE ON FUNCTION" in sql
    assert "public.apply_arena_agent_task_result(TEXT)" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        ARENA_MONEY_PRECISION_POLICY_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_arena_money_precision_recovery_only_requeues_proven_fixed_runs(
    monkeypatch,
):
    sql = ARENA_MONEY_PRECISION_RUN_RECOVERY_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "safe_error_code = 'runtime_moneyerror'" in sql
    assert "result.error_class = 'price_precision_exceeded'" in sql
    assert "market_projection_receipts" in sql
    assert "receipt.result_id IS NULL" in sql
    assert "SET status = 'queued'" in sql
    assert "safe_error_code = NULL" in sql
    assert "completed_at = NULL" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        ARENA_MONEY_PRECISION_RUN_RECOVERY_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_elapsed_task_recovery_only_requeues_proven_precision_followup(
    monkeypatch,
):
    sql = ARENA_ELAPSED_TASK_RUN_RECOVERY_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "safe_error_code = 'runtime_valueerror'" in sql
    assert "result.error_class = 'price_precision_exceeded'" in sql
    assert "task.deadline_at <= clock_timestamp()" in sql
    assert "market_projection_receipts" in sql
    assert "receipt.result_id IS NULL" in sql
    assert "SET status = 'queued'" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        ARENA_ELAPSED_TASK_RUN_RECOVERY_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_projection_collision_recovery_only_requeues_fully_receipted_runs(
    monkeypatch,
):
    sql = ARENA_MARKET_PROJECTION_RUN_RECOVERY_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "safe_error_code = 'runtime_uniqueviolationerror'" in sql
    assert "task.task_kind = 'arena.market.intent'" in sql
    assert "result.apply_status <> 'applied'" in sql
    assert "receipt.result_id IS NULL" in sql
    assert "SET status = 'queued'" in sql
    assert "safe_error_code = NULL" in sql
    assert "completed_at = NULL" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        ARENA_MARKET_PROJECTION_RUN_RECOVERY_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_rfq_projection_recovery_only_requeues_mixed_projection_runs(
    monkeypatch,
):
    sql = MARKET_RFQ_PROJECTION_RUN_RECOVERY_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "safe_error_code = 'runtime_pawnhouserepositoryerror'" in sql
    assert "run.stage = 'match'" in sql
    assert "round.phase = 'match'" in sql
    assert "task.task_kind = 'arena.market.rfq'" in sql
    assert "result.apply_status = 'pending'" in sql
    assert "receipt.result_id IS NOT NULL" in sql
    assert "task.status <> 'completed'" in sql
    assert "result.result_id IS NULL" in sql
    assert "SET status = 'queued'" in sql
    assert "safe_error_code = NULL" in sql
    assert "completed_at = NULL" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        MARKET_RFQ_PROJECTION_RUN_RECOVERY_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_large_foundation_learning_recovery_is_narrow_and_one_time(
    monkeypatch,
):
    sql = HOSTED_LARGE_FOUNDATION_LEARNING_RECOVERY_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "WITH RECURSIVE strategy_lineage" in sql
    assert "job.error_class = 'internal_learning_failure'" in sql
    assert "foundation.source <> 'learned'" in sql
    assert "char_length(foundation.instructions) > 3000" in sql
    assert "job.candidate_strategy_revision_id IS NULL" in sql
    assert "SET status = 'pending'" in sql
    assert "attempt_count = 0" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        HOSTED_LARGE_FOUNDATION_LEARNING_RECOVERY_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_money_precision_function_owner_receives_only_required_updates(
    monkeypatch,
):
    sql = ARENA_MONEY_PRECISION_PRIVILEGES_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "UPDATE (" in sql
    assert "application_outcome" in sql
    assert "applied_action" in sql
    assert "authoritative_entered_at" in sql
    assert "UPDATE (safe_metadata)" in sql
    assert "TO adx_arena_function_owner" in sql
    assert "GRANT UPDATE ON" not in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        ARENA_MONEY_PRECISION_PRIVILEGES_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_precision_privilege_recovery_requires_all_results_unapplied(
    monkeypatch,
):
    sql = ARENA_PRECISION_PRIVILEGE_RUN_RECOVERY_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "safe_error_code = 'runtime_insufficientprivilegeerror'" in sql
    assert "result.apply_status <> 'pending'" in sql
    assert "arena_applied_agent_actions" in sql
    assert "applied.result_id IS NOT NULL" in sql
    assert "SET status = 'queued'" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        ARENA_PRECISION_PRIVILEGE_RUN_RECOVERY_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_phase_d_cutover_retires_only_empty_waiting_fcfs_current_game(
    monkeypatch,
):
    sql = ARENA_PHASE_D_EMPTY_CURRENT_GAME_CUTOVER_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "game.phase = 'registration'" in sql
    assert "game.market_protocol = 'fcfs.v1'" in sql
    assert "NOT EXISTS" in sql
    assert "arena402.game_participants" in sql
    assert "SET phase = 'cancelled'" in sql
    assert "DELETE FROM arena402.current_game" not in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        ARENA_PHASE_D_EMPTY_CURRENT_GAME_CUTOVER_SQL_PATH
        in migrate_module.migration_files("arena")
    )


def test_context_liquidity_recovery_only_requeues_pre_task_decide_failure(
    monkeypatch,
):
    sql = CONTEXT_LIQUIDITY_RUN_RECOVERY_SQL_PATH.read_text(
        encoding="utf-8"
    )

    assert "safe_error_code = 'runtime_undefinedcolumnerror'" in sql
    assert "run.stage = 'decide'" in sql
    assert "round.phase = 'decide'" in sql
    assert "game.phase = 'running'" in sql
    assert "game.market_protocol = 'agent_a2a.v1'" in sql
    assert "NOT EXISTS" in sql
    assert "public.arena_agent_tasks" in sql
    assert "SET status = 'queued'" in sql
    assert "safe_error_code = NULL" in sql
    assert "completed_at = NULL" in sql

    monkeypatch.setenv(
        "ADX_CONNECTOR_MIGRATIONS_DIR",
        str(ROOT / "db" / "migrations"),
    )
    assert (
        CONTEXT_LIQUIDITY_RUN_RECOVERY_SQL_PATH
        in migrate_module.migration_files("arena")
    )
