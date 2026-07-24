from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "005_hosted_agent_production_runtime.sql"
)


def test_production_runtime_migration_adds_read_only_replay_lookup() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")
    assert "FUNCTION lookup_completed_arena_api_idempotency(" in sql
    assert "\nSTABLE\nSECURITY DEFINER" in sql
    assert "TO adx_arena_api;" in sql
    assert "FROM PUBLIC;" in sql


def test_durable_worker_reclaims_expired_running_tasks_without_replay() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")
    assert "FUNCTION claim_hosted_agent_tasks_v2(" in sql
    assert "t.status IN ('leased', 'running')" in sql
    assert "FUNCTION prepare_reclaimed_hosted_task(" in sql
    assert "v_attempt.status = 'request_sent'" in sql
    assert "'request_outcome_unknown'" in sql
    assert "'interrupted_before_send'" in sql
    assert "TO adx_hosted_worker;" in sql


def test_participant_insert_serializes_with_game_start() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")
    assert "FUNCTION enforce_game_agent_open_join()" in sql
    assert "FOR KEY SHARE" in sql
    assert "v_game_status <> 'open'" in sql
    assert "CREATE TRIGGER game_agents_require_open_game" in sql
