from __future__ import annotations

from pathlib import Path

from web.api import _connector_surface_enabled


ROOT = Path(__file__).resolve().parents[1]


def test_connector_surface_flag_defaults_on_and_can_be_disabled(monkeypatch):
    monkeypatch.delenv("ADX_CONNECTOR_SURFACE_ENABLED", raising=False)
    assert _connector_surface_enabled() is True
    monkeypatch.setenv("ADX_CONNECTOR_SURFACE_ENABLED", "false")
    assert _connector_surface_enabled() is False


def test_production_routes_connector_to_one_dedicated_worker():
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    local_compose = (ROOT / "docker-compose.local.yml").read_text(encoding="utf-8")
    mcp_e2e_compose = (ROOT / "tests/docker-compose.mcp-e2e.yml").read_text(
        encoding="utf-8"
    )
    dockerfile = (ROOT / "deploy/docker/Dockerfile.api").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci-cd.yml").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy/scripts/deploy.sh").read_text(encoding="utf-8")
    caddy = (ROOT / "deploy/caddy/Caddyfile.domain").read_text(encoding="utf-8")
    entrypoint = (ROOT / "deploy/scripts/api-entrypoint.sh").read_text(encoding="utf-8")

    assert "  connector-api:\n" in compose
    assert "ADX_ASGI_APP: web.connector_api:create_app" in compose
    assert 'ADX_API_WORKERS: "1"' in compose
    assert 'ADX_CONNECTOR_SURFACE_ENABLED: "false"' in compose
    assert 'ADX_ARENA_MCP_ENABLED: "false"' in compose
    assert "ADX_ARENA_MCP_ENABLED: ${ADX_ARENA_MCP_ENABLED:-false}" in (local_compose)
    assert "${ADX_API_WORKERS:-2}" in compose
    assert caddy.index("@connector path") < caddy.index("@api path")
    assert "/api/auth* /mcp" in caddy
    assert "reverse_proxy connector-api:8000" in caddy
    assert '--workers "${workers}"' in entrypoint
    assert "compose up -d --force-recreate caddy" in deploy
    assert "COPY --chown=adx:adx db_pool_config.py ./db_pool_config.py" in dockerfile
    assert "COPY --chown=adx:adx arena_mcp ./arena_mcp" in dockerfile
    assert "127.0.0.1:18000:8000" in mcp_e2e_compose
    assert 'ADX_ARENA_PAYMENTS_ENABLED: "false"' in mcp_e2e_compose
    assert "Smoke-test production API imports" in workflow
    assert "import db_pool_config; import web.api; import web.connector_api" in workflow
