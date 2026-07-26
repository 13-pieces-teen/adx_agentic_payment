from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SQL = (
    ROOT / "db" / "migrations" / "038_arena_memorial_direct_identity.sql"
).read_text(encoding="utf-8")


def test_direct_identity_migration_accepts_github_and_password_accounts() -> None:
    assert "CREATE OR REPLACE FUNCTION arena402.reconcile_memorial_awards" in SQL
    assert "candidate.temporary = FALSE" in SQL
    assert "candidate.identity_provider IN ('github', 'password')" in SQL
    assert "NEW.identity_provider IN ('github', 'password')" in SQL
    assert "GRANT EXECUTE ON FUNCTION arena402.reconcile_memorial_awards(TEXT)" in SQL


def test_direct_identity_migration_does_not_persist_wallet_secrets() -> None:
    lowered = SQL.lower()
    assert "private_key" not in lowered
    assert "mnemonic" not in lowered
    assert "seed_phrase" not in lowered
    assert "secret_ref" not in lowered
