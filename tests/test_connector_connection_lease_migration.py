from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "db"
    / "migrations"
    / "084_connector_connection_lease_fencing.sql"
).read_text(encoding="utf-8")


def test_connector_connection_lease_schema_and_privileges_are_scoped():
    assert "CREATE TABLE IF NOT EXISTS connector_device_connection_leases" in MIGRATION
    assert "device_id TEXT PRIMARY KEY" in MIGRATION
    assert "instance_id TEXT NOT NULL" in MIGRATION
    assert "fencing_token BIGINT NOT NULL DEFAULT 1" in MIGRATION
    assert "lease_expires_at TIMESTAMPTZ NOT NULL" in MIGRATION
    assert "GRANT SELECT, INSERT, UPDATE ON" in MIGRATION
    assert "TO adx_connector_gateway" in MIGRATION


def test_result_sink_acceptance_is_separate_from_immutable_result_record():
    assert "ADD COLUMN IF NOT EXISTS arena_sink_accepted_at TIMESTAMPTZ" in MIGRATION
    assert "GRANT UPDATE (arena_sink_accepted_at)" in MIGRATION
    assert "connector_agent_task_results" in MIGRATION
