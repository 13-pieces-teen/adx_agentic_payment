"""Schema contract for durable GitHub-backed Arena identities."""

from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "db"
    / "migrations"
    / "014_connector_github_oauth.sql"
)


def test_github_oauth_identity_is_durable_unique_and_api_writable():
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "identity_provider TEXT NOT NULL DEFAULT 'password'" in sql
    assert "provider_subject TEXT" in sql
    assert "identity_provider = 'github'" in sql
    assert "password_hash IS NULL" in sql
    assert "UNIQUE INDEX connector_users_provider_subject_uidx" in sql
    assert "TO adx_connector_gateway" in sql
