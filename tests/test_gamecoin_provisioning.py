from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
DEPLOY = (ROOT / "deploy" / "scripts" / "deploy.sh").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / "deploy" / "env.production.example").read_text(
    encoding="utf-8"
)
GENERATE_ENV = (ROOT / "deploy" / "scripts" / "generate-env.sh").read_text(
    encoding="utf-8"
)
WORKER = (
    ROOT
    / "agent-arena"
    / "settlement"
    / "facilitator"
    / "src"
    / "gamecoin-provisioner.ts"
).read_text(encoding="utf-8")
MIGRATION = (
    ROOT / "db" / "migrations" / "039_arena_game_coin_provisioning.sql"
).read_text(encoding="utf-8")
ARENA_REPOSITORY = (ROOT / "arena_game" / "postgres.py").read_text(
    encoding="utf-8"
)
FACILITATOR = (
    ROOT
    / "agent-arena"
    / "settlement"
    / "facilitator"
    / "src"
    / "index.ts"
).read_text(encoding="utf-8")


def test_gamecoin_provisioner_is_opt_in_and_uses_the_reviewed_secret_mount() -> None:
    service = COMPOSE.split("  gamecoin-provisioner:", 1)[1].split(
        "\n  memorial-minter:", 1
    )[0]
    assert "ADX_ENABLE_GAMECOIN_PROVISIONER" in DEPLOY
    assert "profiles:\n      - gamecoin" in COMPOSE
    assert "facilitators.csv:ro" in COMPOSE
    assert "FACILITATOR_PRIVATE_KEY:" not in COMPOSE
    assert "gamecoin-provisioner-ready" in COMPOSE
    assert "networks:\n      - edge\n      - data" in service


def test_gamecoin_provisioner_persists_before_broadcast_and_recovers() -> None:
    update_at = WORKER.index("UPDATE arena402.game_coin_provisions")
    broadcast_at = WORKER.index("await broadcast(serialized, hash)")
    assert update_at < broadcast_at
    assert "account.signTransaction" in WORKER
    assert "keccak256(serialized)" in WORKER
    assert "getTransactionReceipt" in WORKER
    assert "waitViaBlockscout" in WORKER
    assert "sendRawTransaction" in WORKER
    assert "requiredConfirmations" in WORKER
    assert "game.phase IN ('registration', 'portfolio_setup')" in WORKER


def test_join_waits_for_confirmed_onchain_gamecoin_provisioning() -> None:
    assert "CREATE TABLE arena402.game_coin_provisions" in MIGRATION
    assert "'whitelist_submitted'" in MIGRATION
    assert "'mint_submitted'" in MIGRATION
    assert "'confirmed'" in MIGRATION
    assert "balance_before_atomic" in MIGRATION
    assert "TO adx_arena_core" in MIGRATION
    assert "requires_game_coin_provision" in ARENA_REPOSITORY
    assert "activate_confirmed_game_coin_provisions" in ARENA_REPOSITORY
    assert "participant.settlement_ready" in ARENA_REPOSITORY


def test_facilitator_validates_the_configured_frozen_game_token() -> None:
    assert "ADX_X402_SETTLEMENT_TOKEN_ADDRESS" in FACILITATOR
    assert FACILITATOR.count("tokenAddress: settlementTokenAddress") == 2
    assert (
        "ADX_X402_SETTLEMENT_TOKEN_ADDRESS: "
        "${ADX_CURRENT_GAME_TOKEN_ADDRESS" in COMPOSE
    )


def test_settlement_recovery_uses_the_blockscout_api_origin() -> None:
    expected = "https://testnet.blockscout-api.injective.network/api/v2"
    assert f"ADX_ARENA_SETTLEMENT_BLOCKSCOUT_URL={expected}" in ENV_EXAMPLE
    assert f"ADX_ARENA_SETTLEMENT_BLOCKSCOUT_URL={expected}" in GENERATE_ENV
