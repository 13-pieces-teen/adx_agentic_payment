from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_wallet_configuration_is_forwarded_to_production_api_container() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    api_environment = compose.split(
        "x-api-environment: &api-environment", 1
    )[1].split("\nservices:", 1)[0]
    for name in (
        "ADX_WALLET_RPC_URL",
        "ADX_WALLET_EXPLORER_URL",
        "ADX_ARENA402_G_TOKEN_ADDRESS",
        "ADX_ARENA402_G_TOKEN_DECIMALS",
        "ADX_ARENA402_M_TOKEN_ADDRESS",
        "ADX_ARENA402_M_TOKEN_DECIMALS",
    ):
        assert f"{name}:" in api_environment


def test_wallet_configuration_is_forwarded_to_local_api_container() -> None:
    compose = (ROOT / "docker-compose.local.yml").read_text(encoding="utf-8")
    api_environment = compose.split("\n  api:", 1)[1].split(
        "\n    depends_on:", 1
    )[0]
    assert "ADX_ARENA402_G_TOKEN_ADDRESS:" in api_environment
    assert "ADX_ARENA402_M_TOKEN_ADDRESS:" in api_environment
    assert "ADX_WALLET_RPC_URL:" in api_environment


def test_local_role_provisioning_covers_settlement_and_wallet_roles() -> None:
    compose = (ROOT / "docker-compose.local.yml").read_text(encoding="utf-8")
    role_environment = compose.split(
        "\n  provision-db-roles:", 1
    )[1].split("\n    depends_on:", 1)[0]

    assert "ADX_SETTLEMENT_DATABASE_PASSWORD:" in role_environment
    assert "ADX_WALLET_SIGNER_DATABASE_PASSWORD:" in role_environment
    assert "ADX_WALLET_IMPORTER_DATABASE_PASSWORD:" in role_environment


def test_generated_production_env_contains_wallet_configuration() -> None:
    generator = (
        ROOT / "deploy" / "scripts" / "generate-env.sh"
    ).read_text(encoding="utf-8")
    assert "ADX_WALLET_RPC_URL=https://k8s.testnet.json-rpc.injective.network/" in generator
    assert "ADX_ARENA402_G_TOKEN_ADDRESS=\\n" in generator
    assert "ADX_ARENA402_M_TOKEN_ADDRESS=\\n" in generator


def test_production_settlement_worker_forwards_single_intent_canary() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    settlement_worker = compose.split(
        "\n  settlement-worker:", 1
    )[1].split("\n  wallet-signer:", 1)[0]

    assert "ADX_SETTLEMENT_INTENT_ID: ${ADX_SETTLEMENT_INTENT_ID:-}" in (
        settlement_worker
    )

    generator = (
        ROOT / "deploy" / "scripts" / "generate-env.sh"
    ).read_text(encoding="utf-8")
    assert "ADX_SETTLEMENT_INTENT_ID=\\n" in generator


def test_api_image_contains_wallet_runtime_package() -> None:
    dockerfile = (
        ROOT / "deploy" / "docker" / "Dockerfile.api"
    ).read_text(encoding="utf-8")

    assert "COPY --chown=adx:adx arena_wallets ./arena_wallets" in dockerfile
