"""Static production logging boundaries for secret-bearing Hosted ingress."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_edge_access_logs_never_emit_request_uri_or_query_values() -> None:
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
    for service_block in (
        compose.split("  hosted-worker:", 1)[1].split(
            "\n  credential-controller:", 1
        )[0],
        compose.split("  credential-controller:", 1)[1].split(
            "\n  arena-worker:", 1
        )[0],
        compose.split("  arena-worker:", 1)[1].split(
            "\n  web:", 1
        )[0],
    ):
        assert "\n    ports:" not in service_block
        assert "read_only: true" in service_block
        assert "no-new-privileges:true" in service_block


def test_generated_production_env_has_distinct_role_and_fail_closed_flags() -> None:
    generator = (
        ROOT / "deploy" / "scripts" / "generate-env.sh"
    ).read_text(encoding="utf-8")
    for name in (
        "ADX_API_DATABASE_PASSWORD",
        "ADX_HOSTED_WORKER_DATABASE_PASSWORD",
        "ADX_ARENA_CORE_DATABASE_PASSWORD",
        "ADX_CREDENTIAL_CONTROLLER_DATABASE_PASSWORD",
    ):
        assert f"printf '{name}=%s" in generator
    assert "ADX_HOSTED_AGENTS_ENABLED=false" in generator
    assert "ADX_ENABLE_HOSTED_RUNTIME=false" in generator
    assert "ADX_TENCENT_SSM_IAM_VERIFIED=false" in generator
    assert "ADX_ENABLE_ARENA_WORKER=false" in generator


def test_deploy_script_starts_profiles_only_after_explicit_enablement() -> None:
    deploy = (
        ROOT / "deploy" / "scripts" / "deploy.sh"
    ).read_text(encoding="utf-8")
    assert 'enable_hosted_runtime="$(env_value ADX_ENABLE_HOSTED_RUNTIME)"' in deploy
    assert 'enable_arena_worker="$(env_value ADX_ENABLE_ARENA_WORKER)"' in deploy
    assert (
        'compose --profile hosted up -d hosted-worker '
        "credential-controller"
    ) in deploy
    assert "compose --profile arena up -d arena-worker" in deploy
    assert "verified writer/reader/controller SSM IAM" in deploy
