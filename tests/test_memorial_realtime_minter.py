from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
DEPLOY = (ROOT / "deploy" / "scripts" / "deploy.sh").read_text(encoding="utf-8")
WORKER = (
    ROOT
    / "agent-arena"
    / "settlement"
    / "facilitator"
    / "src"
    / "memorial-minter.ts"
).read_text(encoding="utf-8")
MIGRATION = (
    ROOT / "db" / "migrations" / "037_arena_memorial_realtime_minter.sql"
).read_text(encoding="utf-8")


def test_memorial_minter_is_explicitly_opt_in_and_uses_read_only_secret_mount() -> None:
    service = COMPOSE.split("  memorial-minter:", 1)[1].split(
        "\n  web:", 1
    )[0]
    assert "ADX_ENABLE_MEMORIAL_MINTER" in DEPLOY
    assert '|| enable_memorial_minter=false' in DEPLOY
    assert "profiles:\n      - memorial" in COMPOSE
    assert "facilitators.csv:ro" in COMPOSE
    assert "FACILITATOR_PRIVATE_KEY:" not in COMPOSE
    assert "networks:\n      - edge\n      - data" in service


def test_memorial_minter_serializes_and_recovers_signed_transactions() -> None:
    assert "pg_try_advisory_lock" in WORKER
    assert "account.signTransaction" in WORKER
    assert "tx_nonce" in WORKER
    assert "gas_price_wei" in WORKER
    assert "sendRawTransaction" in WORKER
    assert "getTransactionReceipt" in WORKER
    assert "waitViaBlockscout" in WORKER


def test_memorial_minter_database_access_is_scoped_to_memorial_records() -> None:
    assert "ADD COLUMN tx_nonce BIGINT" in MIGRATION
    assert "TO adx_arena_core" in MIGRATION
    assert "arena402.memorial_awards" in MIGRATION
    assert "arena402.memorial_wallet_inventory" in MIGRATION
