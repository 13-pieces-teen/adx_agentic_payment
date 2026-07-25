from pathlib import Path


SQL = (
    Path(__file__).parents[1]
    / "db"
    / "migrations"
    / "029_arena_external_wallet_binding.sql"
).read_text(encoding="utf-8")


def test_external_wallet_binding_is_separate_from_platform_wallet_inventory() -> None:
    assert "CREATE TABLE arena402.external_wallet_bindings" in SQL
    assert "CREATE TABLE arena402.wallet_binding_challenges" in SQL
    assert "REFERENCES public.connector_users(user_id)" in SQL
    assert "UNIQUE (chain_id, account_address)" in SQL
    assert "message_digest" in SQL
    assert "private" not in SQL.lower()
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in SQL
    assert "CREATE TABLE arena402.user_wallets" not in SQL
