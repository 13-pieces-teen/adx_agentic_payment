from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SQL_PATH = ROOT / "db" / "migrations" / "006_arena_world_game_core.sql"
MARKET_SQL_PATH = (
    ROOT / "db" / "migrations" / "007_arena_market_negotiation.sql"
)
HOSTED_SQL_PATH = (
    ROOT / "db" / "migrations" / "008_arena_pawnhouse_hosted_runtime.sql"
)


def test_world_migration_defines_clean_arena402_authorities() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")
    required = (
        "CREATE SCHEMA IF NOT EXISTS arena402",
        "CREATE TABLE arena402.games",
        "CREATE TABLE arena402.game_goods",
        "CREATE TABLE arena402.game_participants",
        "CREATE TABLE arena402.balances",
        "CREATE TABLE arena402.holdings",
        "CREATE TABLE arena402.event_schedule",
        "CREATE TABLE arena402.event_occurrences",
        "CREATE TABLE arena402.rounds",
        "CREATE TABLE arena402.price_snapshots",
        "CREATE TABLE arena402.game_events",
        "CREATE TABLE arena402.rankings",
    )
    assert all(item in sql for item in required)
    assert "UNIQUE (game_id, user_id)" in sql
    assert "UNIQUE (game_id, agent_id)" in sql
    assert "fixed_trade_quantity = 1" in sql
    assert "NUMERIC(78, 0)" in sql
    assert " REAL" not in sql.upper()
    assert "DOUBLE PRECISION" not in sql.upper()


def test_world_migration_keeps_runtime_roles_least_privileged() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")
    assert "REVOKE ALL ON SCHEMA arena402 FROM PUBLIC" in sql
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA arena402 TO adx_arena_api" in sql
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE\n"
        "    ON ALL TABLES IN SCHEMA arena402 TO adx_arena_core"
    ) in sql
    api_grants = [
        line
        for line in sql.splitlines()
        if "adx_arena_api" in line and "GRANT" in line
    ]
    assert all("INSERT" not in line and "UPDATE" not in line for line in api_grants)


def test_market_migration_defines_fcfs_and_bounded_negotiation_state() -> None:
    sql = MARKET_SQL_PATH.read_text(encoding="utf-8")
    required = (
        "CREATE TABLE arena402.rule_runtime_configs",
        "CREATE TABLE arena402.pool_entries",
        "CREATE TABLE arena402.pairings",
        "CREATE TABLE arena402.negotiations",
        "CREATE TABLE arena402.negotiation_messages",
        "CREATE TABLE arena402.royal_orders",
    )
    assert all(item in sql for item in required)
    assert "result_received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()" in sql
    assert "result_received_at,\n        pool_entry_id" in sql
    assert "max_turns BETWEEN 2 AND 6" in sql
    assert "UNIQUE (negotiation_id, turn_sequence)" in sql
    assert "source_result_id TEXT NOT NULL UNIQUE" in sql
    assert "fixed_trade_quantity" not in sql
    assert " REAL" not in sql.upper()
    assert "DOUBLE PRECISION" not in sql.upper()


def test_hosted_runtime_migration_defines_recoverable_round_run_queue() -> None:
    sql = HOSTED_SQL_PATH.read_text(encoding="utf-8")
    assert "CREATE TABLE arena402.runtime_runs" in sql
    assert "UNIQUE (round_id, runtime_kind)" in sql
    assert "FOR UPDATE" not in sql
    assert "lease_expires_at TIMESTAMPTZ" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON arena402.runtime_runs" in sql
    assert "TO adx_arena_core" in sql
    assert "GRANT SELECT ON arena402.runtime_runs TO adx_arena_api" in sql
