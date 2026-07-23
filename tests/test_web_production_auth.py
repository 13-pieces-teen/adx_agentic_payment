"""Production identity boundaries for the Arena HTTP surface."""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

import web.api as web_api
from connector_gateway.config import ConnectorGatewayConfig
from connector_gateway.production import build_production_connector
from connector_gateway.repository import MemoryConnectorRepository


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _production_app(monkeypatch, invite: str):
    repository = MemoryConnectorRepository()
    config = ConnectorGatewayConfig(
        database_url="postgresql://unused-in-memory",
        session_secret="session-secret-that-is-more-than-32-characters",
        public_app_url="https://arena.example.test",
        bootstrap_invite_hash=_digest(invite),
    )
    bundle = build_production_connector(config, repository)
    monkeypatch.setenv("ADX_ENV", "production")
    monkeypatch.setenv("ADX_PUBLIC_APP_URL", "https://arena.example.test")
    monkeypatch.setattr(web_api, "build_production_connector", lambda: bundle)
    return web_api.create_app(), repository


def _register_user(client: TestClient, invite: str, username: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={
            "invite_code": invite,
            "username": username,
            "password": f"{username}-has-a-long-password",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["csrf_token"]


def _register_agent(client: TestClient, csrf: str, name: str) -> dict:
    response = client.post(
        "/api/agents/register",
        json={
            "name": name,
            "llm_provider": "local",
            "llm_model": "runtime",
            "tradable_assets": ["compute"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_production_arena_mutations_require_session_csrf_and_agent_ownership(
    monkeypatch,
):
    invite = "first-arena-invite-that-is-long-enough"
    app, repository = _production_app(monkeypatch, invite)

    with TestClient(app, base_url="https://arena.example.test") as client:
        anonymous = client.post(
            "/api/agents/register",
            json={"name": "Anonymous"},
        )
        assert anonymous.status_code == 401

        csrf = _register_user(client, invite, "arena-owner")
        missing_csrf = client.post(
            "/api/agents/register",
            json={"name": "Missing CSRF"},
        )
        assert missing_csrf.status_code == 403

        owned_agent = _register_agent(client, csrf, "Owned runtime")
        owner_id = owned_agent["owner_id"]
        assert owner_id.startswith("user_")

        second_invite = "second-arena-invite-that-is-long-enough"
        repository.invites[_digest(second_invite)] = {
            "token_hash": _digest(second_invite),
            "expires_at": None,
            "consumed_at": None,
            "consumed_by": None,
        }
        client.cookies.clear()
        second_csrf = _register_user(client, second_invite, "other-owner")

        agent_id = owned_agent["agent_id"]
        heartbeat = client.post(
            f"/api/agents/{agent_id}/heartbeat",
            headers={"X-CSRF-Token": second_csrf},
        )
        assert heartbeat.status_code == 403

        listing = client.post(
            "/api/listings",
            json={
                "seller_agent_id": agent_id,
                "asset_class": "compute",
                "title": "Forged listing",
            },
            headers={"X-CSRF-Token": second_csrf},
        )
        assert listing.status_code == 403

        intent = client.post(
            "/api/intents",
            json={
                "agent_id": agent_id,
                "intent_type": "sell",
                "asset_class": "compute",
            },
            headers={"X-CSRF-Token": second_csrf},
        )
        assert intent.status_code == 403


def test_legacy_supabase_factory_fails_closed_in_production(monkeypatch):
    monkeypatch.setenv("ADX_ENV", "production")
    try:
        web_api.create_app_with_db(db=object())
    except RuntimeError as exc:
        assert "legacy development factory" in str(exc)
    else:
        raise AssertionError("legacy Supabase factory must not start in production")
