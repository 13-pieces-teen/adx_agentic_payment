from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SQL = (ROOT / "db" / "migrations" / "018_arena_wallet_mandate_x402.sql").read_text(
    encoding="utf-8"
)
CLAIM_PRIVILEGE_SQL = (
    ROOT / "db" / "migrations" / "034_arena_wallet_inventory_claim_privilege.sql"
).read_text(encoding="utf-8")


def test_wallet_binding_is_permanent_and_collision_safe() -> None:
    assert "CREATE TABLE arena402.user_wallets" in SQL
    assert "user_id TEXT PRIMARY KEY" in SQL
    assert "wallet_id TEXT NOT NULL UNIQUE" in SQL
    assert "UNIQUE (github_subject)" in SQL
    assert "UNIQUE (user_id, wallet_id)" in SQL
    assert "UNIQUE (chain_id, account_address)" in SQL
    assert "FOREIGN KEY (wallet_id, chain_id, account_address)" in SQL


def test_wallet_inventory_contains_references_not_private_keys() -> None:
    assert "CREATE TABLE arena402.wallet_inventory" in SQL
    assert "secret_ref TEXT NOT NULL" in SQL
    assert "private_key" not in SQL.lower()
    assert "seed_phrase" not in SQL.lower()


def test_mandate_and_reservation_have_database_enforced_bounds() -> None:
    assert "CREATE TABLE arena402.payment_mandates" in SQL
    assert "max_per_payment_atomic NUMERIC(78, 0)" in SQL
    assert "max_cumulative_atomic NUMERIC(78, 0)" in SQL
    assert "reserved_atomic + consumed_atomic <= max_cumulative_atomic" in SQL
    assert "CREATE UNIQUE INDEX payment_mandates_active_game_uidx" in SQL
    assert "CREATE TABLE arena402.payment_reservations" in SQL
    assert "settlement_intent_id TEXT NOT NULL UNIQUE" in SQL
    assert "UNIQUE (game_id, round_id, buyer_participant_id)" in SQL
    assert "status IN ('reserved', 'submitted', 'consumed', 'released')" in SQL


def test_mandate_is_an_explicit_approval_source() -> None:
    assert "'payment_mandate'" in SQL
    assert "x402_version SMALLINT NOT NULL CHECK (x402_version = 2)" in SQL
    assert "network ~ '^eip155:[1-9][0-9]*$'" in SQL
    assert "CREATE ROLE adx_settlement NOLOGIN" in SQL
    assert "TO adx_settlement" in SQL


def test_settlement_intent_freezes_token_eip712_domain() -> None:
    assert "ADD COLUMN token_eip712_name" in SQL
    assert "ADD COLUMN token_eip712_version" in SQL
    assert "settlement_intents_token_domain_pair" in SQL


def test_wallet_api_can_lock_and_claim_only_inventory_status() -> None:
    assert "GRANT UPDATE (status)" in CLAIM_PRIVILEGE_SQL
    assert "ON arena402.wallet_inventory" in CLAIM_PRIVILEGE_SQL
    assert "TO adx_arena_api" in CLAIM_PRIVILEGE_SQL
    assert "GRANT UPDATE ON" not in CLAIM_PRIVILEGE_SQL
