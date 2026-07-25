"""Least-privilege contract for the durable Connector result outbox."""

from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "db"
    / "migrations"
    / "022_connector_agent_task_result_privileges.sql"
)


def test_gateway_can_recover_and_append_connector_task_results() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "GRANT SELECT, INSERT ON connector_agent_task_results" in sql
    assert "TO adx_connector_gateway;" in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql
