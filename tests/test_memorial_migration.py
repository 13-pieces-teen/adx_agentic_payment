from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SQL = (
    ROOT / "db" / "migrations" / "035_arena_memorial_awards.sql"
).read_text(encoding="utf-8")


def test_memorial_schema_freezes_exactly_402_github_registrations() -> None:
    assert "CREATE TABLE arena402.memorial_campaigns" in SQL
    assert "CREATE TABLE arena402.memorial_wallet_inventory" in SQL
    assert "CREATE TABLE arena402.memorial_awards" in SQL
    assert "max_supply INTEGER NOT NULL CHECK (max_supply = 402)" in SQL
    assert "CHECK (token_id = registration_rank - 1)" in SQL
    assert "candidate.identity_provider = 'github'" in SQL
    assert "candidate.temporary = FALSE" in SQL
    assert "ORDER BY candidate.created_at, candidate.user_id" in SQL
    assert "FOR UPDATE;" in SQL
    assert "connector_user_memorial_after_insert" in SQL
    assert (
        "GRANT SELECT, TRIGGER ON TABLE public.connector_users"
        in SQL
    )
    assert "REVOKE TRIGGER ON TABLE public.connector_users" in SQL


def test_memorial_business_schema_never_persists_wallet_secrets() -> None:
    lowered = SQL.lower()
    assert "private_key" not in lowered
    assert "mnemonic" not in lowered
    assert "seed_phrase" not in lowered
    assert "secret_ref" not in lowered
    assert "grant select on" in lowered
    assert "to adx_arena_api" in lowered
