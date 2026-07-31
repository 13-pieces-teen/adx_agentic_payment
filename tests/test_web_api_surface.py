"""Regression tests for the current Arena 402 API composition root."""

from fastapi.testclient import TestClient

from web.api import create_app


LEGACY_PATHS = {
    "/api/agents",
    "/api/agents/register",
    "/api/listings",
    "/api/intents",
    "/api/negotiations/start",
    "/api/arena/leaderboard",
    "/api/arena/battles",
}


def test_default_surface_exposes_current_capabilities_and_health(monkeypatch):
    monkeypatch.delenv("ADX_CONNECTOR_UNSAFE_DEMO", raising=False)
    monkeypatch.delenv("ADX_CONNECTOR_MODE", raising=False)
    monkeypatch.delenv("ADX_ENV", raising=False)
    monkeypatch.delenv("ADX_HOSTED_AGENTS_ENABLED", raising=False)
    monkeypatch.delenv("ADX_ARENA_PARTICIPATION_ENABLED", raising=False)
    monkeypatch.delenv("ADX_ARENA_CORE_ENABLED", raising=False)
    monkeypatch.delenv("ADX_ARENA_PAYMENTS_ENABLED", raising=False)
    monkeypatch.delenv("ADX_ARENA_MEMORIAL_ENABLED", raising=False)
    monkeypatch.delenv("ADX_ARENA_DEV_CONTROL", raising=False)
    monkeypatch.delenv("ADX_ARENA_MCP_ENABLED", raising=False)

    client = TestClient(create_app())
    health = client.get("/api/health")
    ready = client.get("/api/ready")
    metrics = client.get("/metrics")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "version": "0.3.0",
        "connector_gateway": "off",
        "hosted_agent_creation": False,
        "arena_participation": False,
        "arena_payments": False,
        "arena_memorial": False,
        "arena_mcp": False,
        "pawnhouse": "off",
    }
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "dependencies": {}}
    assert "arena_http_requests_total" in metrics.text
    assert "arena_http_request_duration_seconds" in metrics.text
    assert client.get("/api/hosted-agents/capabilities").status_code == 200


def test_legacy_matching_routes_are_not_mounted(monkeypatch):
    monkeypatch.delenv("ADX_CONNECTOR_MODE", raising=False)
    monkeypatch.delenv("ADX_ENV", raising=False)
    app = create_app(connector_demo_enabled=False)
    paths = set(app.openapi()["paths"])

    assert LEGACY_PATHS.isdisjoint(paths)
    for path in LEGACY_PATHS:
        assert TestClient(app).get(path).status_code in {404, 405}
