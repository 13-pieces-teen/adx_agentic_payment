from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.testclient import TestClient

from connector_gateway.auth import AuthError
from web.api import _connector_surface_enabled
from web.connector_api import create_app as create_connector_app


ROOT = Path(__file__).resolve().parents[1]


def test_connector_surface_flag_defaults_on_and_can_be_disabled(monkeypatch):
    monkeypatch.delenv("ADX_CONNECTOR_SURFACE_ENABLED", raising=False)
    assert _connector_surface_enabled() is True
    monkeypatch.setenv("ADX_CONNECTOR_SURFACE_ENABLED", "false")
    assert _connector_surface_enabled() is False


def test_production_routes_connector_to_one_dedicated_worker():
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    local_compose = (ROOT / "docker-compose.local.yml").read_text(encoding="utf-8")
    mcp_e2e_compose = (ROOT / "tests/e2e/docker-compose.mcp-e2e.yml").read_text(
        encoding="utf-8"
    )
    dockerfile = (ROOT / "deploy/docker/Dockerfile.api").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci-cd.yml").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy/scripts/deploy.sh").read_text(encoding="utf-8")
    caddy = (ROOT / "deploy/caddy/Caddyfile.domain").read_text(encoding="utf-8")
    ip_caddy = (ROOT / "deploy/caddy/Caddyfile.ip").read_text(encoding="utf-8")
    connector_api = (ROOT / "web/connector_api.py").read_text(encoding="utf-8")
    entrypoint = (ROOT / "deploy/scripts/api-entrypoint.sh").read_text(encoding="utf-8")

    assert "  connector-api:\n" in compose
    assert "ADX_ASGI_APP: web.connector_api:create_app" in compose
    assert 'ADX_API_WORKERS: "1"' in compose
    assert 'ADX_CONNECTOR_SURFACE_ENABLED: "false"' in compose
    assert 'ADX_ARENA_MCP_ENABLED: "false"' in compose
    assert "ADX_ARENA_MCP_ENABLED: ${ADX_ARENA_MCP_ENABLED:-false}" in (local_compose)
    assert "${ADX_API_WORKERS:-2}" in compose
    assert caddy.index("@connector path") < caddy.index("@api path")
    assert "/api/auth*" in caddy
    assert "/mcp" in caddy
    assert "/api/v1/admin/current-game /api/v1/admin/current-game/matchmaking" in (
        caddy
    )
    assert "/api/v1/admin/current-game /api/v1/admin/current-game/matchmaking" in (
        ip_caddy
    )
    assert "reverse_proxy connector-api:8000" in caddy
    assert "create_current_game_admin_router" in connector_api
    assert '"PUT"' in connector_api
    assert '--workers "${workers}"' in entrypoint
    assert "compose up -d --force-recreate caddy" in deploy
    assert "COPY --chown=adx:adx db_pool_config.py ./db_pool_config.py" in dockerfile
    assert "COPY --chown=adx:adx arena_mcp ./arena_mcp" in dockerfile
    assert "127.0.0.1:18000:8000" in mcp_e2e_compose
    assert 'ADX_ARENA_PAYMENTS_ENABLED: "false"' in mcp_e2e_compose
    assert "Smoke-test production API imports" in workflow
    assert "import db_pool_config; import web.api; import web.connector_api" in workflow


def test_dedicated_connector_app_mounts_current_game_admin_routes(
    monkeypatch,
):
    class _Auth:
        async def authenticate(self, _request):
            raise AuthError(401, "Authentication required")

    class _Service:
        def bind_agent_task_result_sink(self, _sink):
            return None

    class _Bundle:
        def __init__(self):
            self.auth = _Auth()
            self.repository = object()
            self.router = APIRouter()
            self.service = _Service()

        async def initialize(self):
            return None

        async def close(self):
            return None

    monkeypatch.setenv("ADX_ENV", "production")
    monkeypatch.setenv("ADX_ALLOWED_ORIGINS", "https://www.arena402.com")
    monkeypatch.setenv("ADX_ARENA_API_DATABASE_URL", "postgresql://test/api")
    monkeypatch.setenv("ADX_ARENA_CORE_DATABASE_URL", "postgresql://test/core")
    monkeypatch.setattr(
        "web.connector_api.build_production_connector",
        lambda **_kwargs: _Bundle(),
    )

    app = create_connector_app()
    routes = list(app.routes)
    for included in app.routes:
        original_router = getattr(included, "original_router", None)
        if original_router is not None:
            routes.extend(original_router.routes)
    methods_by_path = {
        route.path: route.methods
        for route in routes
        if hasattr(route, "path") and hasattr(route, "methods")
    }

    assert "/api/v1/admin/current-game" in methods_by_path, methods_by_path
    assert methods_by_path["/api/v1/admin/current-game"] == {"GET"}
    assert methods_by_path["/api/v1/admin/current-game/matchmaking"] == {"PUT"}
    response = TestClient(app).get("/api/v1/admin/current-game")
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}
