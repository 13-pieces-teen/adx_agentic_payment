"""Static production logging boundaries for secret-bearing Hosted ingress."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    assert 'enable_arena_worker="$(env_value ADX_ENABLE_ARENA_WORKER)"' in deploy
    assert (
        'enable_settlement_worker="$(env_value ADX_ENABLE_SETTLEMENT_WORKER)"'
        in deploy
    )
    assert (
        'compose --profile hosted up -d --scale hosted-worker="'
        '${hosted_worker_replicas}" hosted-worker credential-controller'
    ) in deploy
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
    assert compose.count("target: /run/secrets/arena402") == 3
    assert compose.count(
        "ADX_HOSTED_MASTER_KEY_FILE: "
        "/run/secrets/arena402/hosted-master.key"
    ) == 3
    bootstrap = compose.split("  official-agent-bootstrap:", 1)[1].split(
        "\n  arena-worker:", 1
    )[0]
    assert "profiles:\n      - ops" in bootstrap
    assert "target: /run/secrets/arena402" in bootstrap
    assert "read_only: true" in bootstrap
    controller = compose.split("  credential-controller:", 1)[1].split(
        "\n  official-agent-bootstrap:", 1
    )[0]
    assert "ADX_HOSTED_MASTER_KEY_FILE" not in controller
    assert "target: /run/secrets/arena402" not in controller


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
    assert "profiles:\n      - legacy-web" in compose
    assert "compose build --pull api migrate provision-db-roles" in deploy
    assert "compose up -d api caddy" in deploy
    assert "compose up -d api web caddy" not in deploy
    assert "--app-url" in generator
    assert 'printf \'ADX_GITHUB_OAUTH_CLIENT_ID=\\n\'' in generator
    assert 'printf \'ADX_GITHUB_OAUTH_CLIENT_SECRET=\\n\'' in generator
    assert 'printf \'ADX_GITHUB_OAUTH_RELAY_URL=\\n\'' in generator
    assert "redir {$ADX_PUBLIC_APP_URL}{uri} permanent" in caddy
    assert "reverse_proxy web:3000" not in caddy
