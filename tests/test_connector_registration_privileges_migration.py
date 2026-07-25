"""Least-privilege contract for Connector-to-Arena registration replay."""

from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "db"
    / "migrations"
    / "023_arena_connector_registration_privileges.sql"
)


def test_api_can_advance_only_the_connector_binding_epoch() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "GRANT UPDATE (connector_binding_epoch)" in sql
    assert "ON arena_runtime_bindings" in sql
    assert "TO adx_arena_api;" in sql
    assert "GRANT UPDATE ON arena_runtime_bindings" not in sql
