"""Static production logging boundaries for secret-bearing Hosted ingress."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_litellm_vault_import_does_not_require_pydantic_ai() -> None:
    guarded_import = """
import builtins
real_import = builtins.__import__
def guard(name, *args, **kwargs):
    if name == "pydantic_ai" or name.startswith("pydantic_ai."):
        raise RuntimeError("pydantic_ai must stay outside the vault bootstrap")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guard
from hosted_agent_runtime.production_secrets import configured_backend
assert callable(configured_backend)
"""
    result = subprocess.run(
        [sys.executable, "-c", guarded_import],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_edge_access_logs_never_emit_oauth_urls_or_query_values() -> None:
    for name in (
        "Caddyfile.domain",
        "Caddyfile.ip",
        "Caddyfile.ip-bootstrap",
    ):
        config = (ROOT / "deploy" / "caddy" / name).read_text(
            encoding="utf-8"
        )
        assert "format filter {" in config
        assert "request>uri replace REDACTED" in config
        assert "request>headers delete" in config
        assert "resp_headers delete" in config
        assert "wrap json" in config


def test_uvicorn_access_log_is_disabled_behind_redacting_edge() -> None:
    entrypoint = (
        ROOT / "deploy" / "scripts" / "api-entrypoint.sh"
    ).read_text(encoding="utf-8")
    assert "--no-access-log" in entrypoint
    assert "\n  --access-log" not in entrypoint


def test_production_workers_are_separate_non_public_services() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(
        encoding="utf-8"
    )
    assert "hosted-worker:" in compose
    assert "credential-controller:" in compose
    assert "arena-worker:" in compose
    assert "settlement-worker:" in compose
    assert (
        'command: ["python", "-m", '
        '"hosted_agent_runtime.production_worker"]'
    ) in compose
    assert (
        '["python", "-m", '
        '"hosted_agent_control_plane.production_controller"]'
    ) in compose
    assert (
        'command: ["python", "-m", "arena_game.production_worker"]'
    ) in compose
    assert "arena_payments.production_service:create_app" in compose
    for service_block in (
        compose.split("  hosted-worker:", 1)[1].split(
            "\n  credential-controller:", 1
        )[0],
        compose.split("  credential-controller:", 1)[1].split(
            "\n  arena-worker:", 1
        )[0],
        compose.split("  arena-worker:", 1)[1].split(
            "\n  settlement-worker:", 1
        )[0],
        compose.split("  settlement-worker:", 1)[1].split(
            "\n  wallet-signer:", 1
        )[0],
    ):
        assert "\n    ports:" not in service_block
        assert "read_only: true" in service_block
        assert "no-new-privileges:true" in service_block
    arena_block = compose.split("  arena-worker:", 1)[1].split(
        "\n  settlement-worker:", 1
    )[0]
    assert "ADX_WALLET_SIGNER_TOKEN" not in arena_block
    assert "ADX_X402_FACILITATOR_AUTHORIZATION" not in arena_block
    settlement_block = compose.split("  settlement-worker:", 1)[1].split(
        "\n  wallet-signer:", 1
    )[0]
    assert "adx_settlement_login" in settlement_block
    assert "ADX_WALLET_SIGNER_TOKEN" in settlement_block
    assert "ADX_ARENA_SETTLEMENT_BLOCKSCOUT_URL" in settlement_block
    for source_path in (
        ROOT / "arena_payments" / "production_worker.py",
        ROOT / "arena_payments" / "production_service.py",
    ):
        source = source_path.read_text(encoding="utf-8")
        assert 'blockscout_base_url=_https_url(' in source
        assert '"ADX_ARENA_SETTLEMENT_BLOCKSCOUT_URL"' in source
    api_environment = compose.split(
        "x-api-environment: &api-environment", 1
    )[1].split("\nservices:", 1)[0]
    assert "ADX_WALLET_SIGNER_TOKEN" not in api_environment
    assert "ADX_X402_FACILITATOR_AUTHORIZATION" not in api_environment
    assert "ADX_SETTLEMENT_SERVICE_URL" in api_environment


def test_production_uses_four_independent_facilitator_shards() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(
        encoding="utf-8"
    )
    deploy = (ROOT / "deploy" / "scripts" / "deploy.sh").read_text(
        encoding="utf-8"
    )

    for index in range(1, 5):
        assert f"  arena-facilitator-{index}:" in compose
        assert (
            f"ADX_FACILITATOR_WALLET_INDEX: "
            f'${{ADX_FACILITATOR_{index}_WALLET_INDEX:-{index}}}'
        ) in compose
        assert f"ADX_X402_FACILITATOR_{index}_URL:" in compose
        assert f"ADX_X402_FACILITATOR_{index}_ID:" in compose
        assert f"ADX_X402_FACILITATOR_{index}_EOA:" in compose
        assert f"ADX_X402_FACILITATOR_{index}_AUTHORIZATION:" in compose
    assert "ADX_X402_FACILITATOR_SHARD_COUNT: ${" in compose
    assert "ADX_SETTLEMENT_WORKER_CONCURRENCY: ${" in compose
    assert (
        "compose --profile testnet-facilitator up -d "
        "arena-facilitator-1 arena-facilitator-2 "
        "arena-facilitator-3 arena-facilitator-4"
    ) in deploy
    assert "Facilitator bearer tokens must be unique." in deploy
    assert "Facilitator wallet indices must be unique." in deploy
    assert "Facilitator EOA addresses must be unique." in deploy
    assert 'set_env_value "ADX_X402_FACILITATOR_${facilitator_index}_EOA"' in deploy
    assert '"ethereum_address"' in deploy
    assert (
        "ADX_X402_FACILITATOR_SHARD_COUNT must be an integer."
        in deploy
    )
    assert (
        "Facilitator CSV must contain exactly one row for wallet index"
        in deploy
    )


def test_generated_production_env_has_distinct_role_and_fail_closed_flags() -> None:
    generator = (
        ROOT / "deploy" / "scripts" / "generate-env.sh"
    ).read_text(encoding="utf-8")
    for name in (
        "ADX_API_DATABASE_PASSWORD",
        "ADX_HOSTED_WORKER_DATABASE_PASSWORD",
        "ADX_ARENA_CORE_DATABASE_PASSWORD",
        "ADX_SETTLEMENT_DATABASE_PASSWORD",
        "ADX_CREDENTIAL_CONTROLLER_DATABASE_PASSWORD",
    ):
        assert f"printf '{name}=%s" in generator
    assert "ADX_HOSTED_AGENTS_ENABLED=false" in generator
    assert "ADX_ARENA_CORE_ENABLED=true" in generator
    assert "ADX_ENABLE_HOSTED_RUNTIME=false" in generator
    assert "ADX_ENABLE_OFFICIAL_LITELLM=false" in generator
    assert "ADX_HOSTED_SECRET_BACKEND=postgres_aesgcm" in generator
    assert "ADX_HOSTED_CREDENTIAL_BACKEND_VERIFIED=false" in generator
    assert "ADX_TENCENT_SSM_IAM_VERIFIED=false" in generator
    assert "ADX_ENABLE_ARENA_WORKER=true" in generator
    assert "ADX_ENABLE_SETTLEMENT_WORKER=false" in generator
    assert "ADX_SETTLEMENT_SERVICE_TOKEN=%s" in generator


def test_deploy_script_starts_profiles_only_after_explicit_enablement() -> None:
    deploy = (
        ROOT / "deploy" / "scripts" / "deploy.sh"
    ).read_text(encoding="utf-8")
    assert 'enable_hosted_runtime="$(env_value ADX_ENABLE_HOSTED_RUNTIME)"' in deploy
    assert (
        'enable_official_litellm="$(env_value ADX_ENABLE_OFFICIAL_LITELLM)"'
        in deploy
    )
    assert 'enable_arena_worker="$(env_value ADX_ENABLE_ARENA_WORKER)"' in deploy
    assert (
        'enable_settlement_worker="$(env_value ADX_ENABLE_SETTLEMENT_WORKER)"'
        in deploy
    )
    assert (
        'compose --profile hosted up -d --scale hosted-worker="'
        '${hosted_worker_replicas}" hosted-worker credential-controller'
    ) in deploy
    assert (
        "compose --profile official-agents build --pull official-litellm"
        in deploy
    )
    assert (
        "compose --profile official-agents up -d --force-recreate "
        "official-litellm"
    ) in deploy
    assert "Missing Official LiteLLM manifest." in deploy
    assert "compose --profile arena up -d arena-worker" in deploy
    assert "compose --profile settlement up -d settlement-worker" in deploy
    assert "Arena Worker requires ADX_ARENA_CORE_ENABLED=true." in deploy
    assert "PostgreSQL AES-GCM Hosted credentials" in deploy
    assert "Tencent SSM Hosted credentials" in deploy
    assert "hosted-master.key" in deploy
    assert "ADX_HOSTED_WORKER_REPLICAS" in deploy


def test_hosted_master_key_is_mounted_only_into_approved_secret_processes() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(
        encoding="utf-8"
    )
    dockerfile = (
        ROOT / "deploy" / "docker" / "Dockerfile.api"
    ).read_text(encoding="utf-8")
    assert compose.count("target: /run/secrets/arena402") == 5
    assert compose.count(
        "ADX_HOSTED_MASTER_KEY_FILE: "
        "/run/secrets/arena402/hosted-master.key"
    ) == 5
    bootstrap = compose.split("  official-agent-bootstrap:", 1)[1].split(
        "\n  official-agent-strategy-refresh:", 1
    )[0]
    assert "profiles:\n      - ops" in bootstrap
    assert "- scripts.bootstrap_official_agent_pool" in bootstrap
    assert "- --litellm-token-file" in bootstrap
    assert "- /run/secrets/official/litellm-token.key" in bootstrap
    assert "ADX_OFFICIAL_DEEPSEEK_KEY_SOURCE_HOST_PATH" not in bootstrap
    assert "target: /run/secrets/official/deepseek-source" not in bootstrap
    assert "target: /run/secrets/official/litellm-token.key" in bootstrap
    assert "--config-version" in bootstrap
    assert "ADX_OFFICIAL_LITELLM_CONFIG_VERSION" in bootstrap
    assert bootstrap.count("create_host_path: false") == 1
    assert "target: /run/secrets/arena402" in bootstrap
    assert "read_only: true" in bootstrap
    bootstrap_environment = bootstrap.split("    environment:", 1)[1].split(
        "    depends_on:", 1
    )[0]
    assert "ADX_OFFICIAL_DEEPSEEK" not in bootstrap_environment
    strategy_refresh = compose.split(
        "  official-agent-strategy-refresh:", 1
    )[1].split("\n  arena-worker:", 1)[0]
    assert "- scripts.refresh_official_agent_strategies" in strategy_refresh
    assert "litellm-token.key" not in strategy_refresh
    assert "target: /run/secrets/arena402" not in strategy_refresh
    assert "ADX_HOSTED_MASTER_KEY_FILE" not in strategy_refresh
    assert (
        "COPY --chown=adx:adx scripts/bootstrap_official_agent_pool.py "
        "./scripts/bootstrap_official_agent_pool.py"
    ) in dockerfile
    assert (
        "COPY --chown=adx:adx scripts/provision_official_litellm.py "
        "./scripts/provision_official_litellm.py"
    ) in dockerfile
    assert (
        "COPY --chown=adx:adx scripts/refresh_official_agent_strategies.py "
        "./scripts/refresh_official_agent_strategies.py"
    ) in dockerfile
    controller = compose.split("  credential-controller:", 1)[1].split(
        "\n  official-litellm:", 1
    )[0]
    assert "ADX_HOSTED_MASTER_KEY_FILE" not in controller
    assert "target: /run/secrets/arena402" not in controller


def test_official_litellm_is_private_and_owns_upstream_distribution() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(
        encoding="utf-8"
    )
    dockerfile = (
        ROOT / "deploy" / "docker" / "Dockerfile.litellm"
    ).read_text(encoding="utf-8")
    proxy = compose.split("  official-litellm:", 1)[1].split(
        "\n  official-litellm-provision:", 1
    )[0]
    provision = compose.split("  official-litellm-provision:", 1)[1].split(
        "\n  official-agent-bootstrap:", 1
    )[0]
    hosted_worker = compose.split("  hosted-worker:", 1)[1].split(
        "\n  credential-controller:", 1
    )[0]

    assert "profiles:\n      - official-agents" in proxy
    assert "      - ops" in proxy
    assert "\n    ports:" not in proxy
    assert 'expose:\n      - "4000"' in proxy
    assert "- official-llm" in proxy
    assert "read_only: true" in proxy
    assert "cap_drop:\n      - ALL" in proxy
    assert "target: /run/official-litellm" in proxy
    assert "deepseek-source" not in proxy
    assert "litellm-token.key" not in proxy
    assert "ADX_LITELLM_SECRET_DATABASE_URL" in proxy
    assert "- official-llm" in hosted_worker

    assert "- scripts.provision_official_litellm" in provision
    assert "target: /run/secrets/official/deepseek-source" in provision
    assert "target: /run/secrets/official/litellm-token.key" in provision
    assert "target: /run/official-litellm" in provision
    assert "--config-version" in provision
    assert "ADX_OFFICIAL_LITELLM_CONFIG_VERSION" in provision
    assert "ADX_OFFICIAL_BOOTSTRAP_DATABASE_URL" not in provision
    assert "ADX_HOSTED_FINGERPRINT_PEPPER" not in provision
    assert "create_host_path: false" in provision

    assert (
        "ghcr.io/berriai/litellm-non_root:v1.89.4@sha256:"
        "2cf7711f9e96f9b2b3f4a2ba9eeaa39ab229020983b85e6f6723d8827b4df209"
    ) in dockerfile
    assert (
        'ENTRYPOINT ["python", "-m", "scripts.run_official_litellm"]'
        in dockerfile
    )
    assert "USER 10001:10001" in dockerfile


def test_production_deploy_targets_external_frontend_and_github_oauth() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(
        encoding="utf-8"
    )
    deploy = (ROOT / "deploy" / "scripts" / "deploy.sh").read_text(
        encoding="utf-8"
    )
    generator = (
        ROOT / "deploy" / "scripts" / "generate-env.sh"
    ).read_text(encoding="utf-8")
    caddy = (ROOT / "deploy" / "caddy" / "Caddyfile.domain").read_text(
        encoding="utf-8"
    )

    assert "ADX_GITHUB_OAUTH_CLIENT_ID:" in compose
    assert "ADX_GITHUB_OAUTH_CLIENT_SECRET:" in compose
    assert "ADX_GITHUB_OAUTH_RELAY_URL:" in compose
    assert "\n  web:" not in compose
    assert "legacy-web" not in compose
    assert "Dockerfile.web" not in compose
    assert (
        "compose build --pull api connector-api migrate provision-db-roles"
        in deploy
    )
    assert "compose up -d api connector-api" in deploy
    assert "compose up -d --force-recreate caddy" in deploy
    assert "compose up -d api web caddy" not in deploy
    assert "--app-url" in generator
    assert 'printf \'ADX_GITHUB_OAUTH_CLIENT_ID=\\n\'' in generator
    assert 'printf \'ADX_GITHUB_OAUTH_CLIENT_SECRET=\\n\'' in generator
    assert 'printf \'ADX_GITHUB_OAUTH_RELAY_URL=\\n\'' in generator
    assert "redir {$ADX_PUBLIC_APP_URL}{uri} permanent" in caddy
    assert "reverse_proxy web:3000" not in caddy
