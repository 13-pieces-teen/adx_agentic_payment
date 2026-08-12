"""Security and durability contracts for the production Connector Gateway."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from connector_gateway.config import (
    ConnectorConfigurationError,
    ConnectorGatewayConfig,
)
from connector_gateway.models import CommandAction, RuntimeInventoryItem
from connector_gateway.persistent_service import PersistentConnectorGateway
from connector_gateway.production import build_production_connector
from connector_gateway.repository import MemoryConnectorRepository
from connector_gateway.service import ConnectionReplacedError, ConnectorError


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bundle(
    invite: str = "invite-one-that-is-long-enough",
    *,
    auth_rate_limit_attempts: int = 10,
    pairing_rate_limit_attempts: int = 60,
    max_pending_pairings: int = 500,
    public_registration_enabled: bool = False,
    repository: MemoryConnectorRepository | None = None,
):
    repository = repository or MemoryConnectorRepository()
    config = ConnectorGatewayConfig(
        database_url="postgresql://unused-in-memory",
        session_secret="session-secret-that-is-more-than-32-characters",
        public_app_url="https://arena.example.test",
        bootstrap_invite_hash=_hash(invite),
        public_registration_enabled=public_registration_enabled,
        auth_rate_limit_attempts=auth_rate_limit_attempts,
        pairing_rate_limit_attempts=pairing_rate_limit_attempts,
        max_pending_pairings=max_pending_pairings,
    )
    bundle = build_production_connector(config, repository)
    app = FastAPI()
    app.include_router(bundle.router)
    return bundle, repository, TestClient(app, base_url="https://testserver")


def _invite(
    client: TestClient,
    invite_code: str,
    username: str | None = None,
    password: str | None = None,
) -> str:
    body: dict[str, str] = {"invite_code": invite_code}
    body["username"] = username or f"user-{_hash(invite_code)[:12]}"
    body["password"] = password or "correct horse battery staple"
    response = client.post("/api/auth/invite", json=body)
    assert response.status_code == 201, response.text
    return response.json()["csrf_token"]


def _create_pairing(client: TestClient):
    response = client.post(
        "/api/connectors/pairings",
        json={"device_name": "Production laptop", "owner_id": "forged-owner"},
    )
    assert response.status_code == 201
    return response.json()


def test_production_configuration_fails_closed(monkeypatch):
    for name in (
        "ADX_CONNECTOR_DATABASE_URL",
        "DATABASE_URL",
        "ADX_CONNECTOR_SESSION_SECRET",
        "ADX_PUBLIC_APP_URL",
        "ADX_BOOTSTRAP_INVITE_HASH",
        "ADX_GITHUB_OAUTH_CLIENT_ID",
        "ADX_GITHUB_OAUTH_CLIENT_SECRET",
        "ADX_GITHUB_OAUTH_RELAY_URL",
        "ADX_PUBLIC_REGISTRATION_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConnectorConfigurationError, match="DATABASE_URL"):
        ConnectorGatewayConfig.from_env()

    monkeypatch.setenv("DATABASE_URL", "postgresql://db/arena")
    monkeypatch.setenv("ADX_CONNECTOR_SESSION_SECRET", "x" * 40)
    monkeypatch.setenv("ADX_PUBLIC_APP_URL", "http://arena.example.test")
    monkeypatch.setenv("ADX_BOOTSTRAP_INVITE_HASH", _hash("i" * 24))
    with pytest.raises(ConnectorConfigurationError, match="HTTPS"):
        ConnectorGatewayConfig.from_env()

    monkeypatch.setenv("ADX_PUBLIC_APP_URL", "https://arena.example.test")
    config = ConnectorGatewayConfig.from_env()
    assert config.session_cookie_name == "adx_session"
    assert config.csrf_cookie_name == "adx_csrf"
    assert config.public_registration_enabled is True

    monkeypatch.delenv("ADX_BOOTSTRAP_INVITE_HASH")
    config = ConnectorGatewayConfig.from_env()
    assert config.public_registration_enabled is True
    assert config.bootstrap_invite_hash is None

    monkeypatch.setenv("ADX_PUBLIC_REGISTRATION_ENABLED", "sometimes")
    with pytest.raises(ConnectorConfigurationError, match="must be one of"):
        ConnectorGatewayConfig.from_env()
    monkeypatch.setenv("ADX_PUBLIC_REGISTRATION_ENABLED", "true")

    monkeypatch.setenv("ADX_GITHUB_OAUTH_CLIENT_ID", "github-client-id")
    with pytest.raises(ConnectorConfigurationError, match="configured together"):
        ConnectorGatewayConfig.from_env()
    monkeypatch.setenv("ADX_GITHUB_OAUTH_CLIENT_SECRET", "github-client-secret")
    config = ConnectorGatewayConfig.from_env()
    assert config.github_oauth_client_id == "github-client-id"

    monkeypatch.setenv(
        "ADX_GITHUB_OAUTH_RELAY_URL",
        "http://www.arena402.com/api/internal/github/oauth",
    )
    with pytest.raises(ConnectorConfigurationError, match="must use HTTPS"):
        ConnectorGatewayConfig.from_env()
    monkeypatch.setenv(
        "ADX_GITHUB_OAUTH_RELAY_URL",
        "https://www.arena402.com/api/internal/github/oauth",
    )
    config = ConnectorGatewayConfig.from_env()
    assert config.github_oauth_relay_url == (
        "https://www.arena402.com/api/internal/github/oauth"
    )


def test_github_sign_in_starts_same_origin_pkce_flow():
    config = ConnectorGatewayConfig(
        database_url="postgresql://unused-in-memory",
        session_secret="session-secret-that-is-more-than-32-characters",
        public_app_url="https://arena402.com",
        github_oauth_client_id="github-client-id",
        github_oauth_client_secret="github-client-secret",
    )
    bundle = build_production_connector(config, MemoryConnectorRepository())
    app = FastAPI()
    app.include_router(bundle.router)
    client = TestClient(app, base_url="https://arena402.com")

    response = client.get(
        "/api/auth/github/start",
        params={"return_to": "/agents?tab=hosted"},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["cache-control"] == "no-store"
    location = urlparse(response.headers["location"])
    assert (location.scheme, location.netloc, location.path) == (
        "https",
        "github.com",
        "/login/oauth/authorize",
    )
    query = parse_qs(location.query)
    assert query["client_id"] == ["github-client-id"]
    assert query["redirect_uri"] == [
        "https://arena402.com/api/auth/github/callback"
    ]
    assert query["scope"] == ["read:user"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    assert len(query["state"][0]) >= 32
    state_cookie = response.headers["set-cookie"]
    assert "adx_github_oauth_state=" in state_cookie
    assert "HttpOnly" in state_cookie
    assert "Secure" in state_cookie
    assert "SameSite=lax" in state_cookie


def test_github_sign_in_uses_configured_api_callback_origin():
    config = ConnectorGatewayConfig(
        database_url="postgresql://unused-in-memory",
        session_secret="session-secret-that-is-more-than-32-characters",
        public_app_url="https://www.arena402.com",
        github_oauth_callback_base_url="https://api.arena402.com",
        github_oauth_client_id="github-client-id",
        github_oauth_client_secret="github-client-secret",
    )
    bundle = build_production_connector(config, MemoryConnectorRepository())
    app = FastAPI()
    app.include_router(bundle.router)
    client = TestClient(app, base_url="https://api.arena402.com")

    response = client.get("/api/auth/github/start", follow_redirects=False)

    assert response.status_code == 307
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["redirect_uri"] == [
        "https://api.arena402.com/api/auth/github/callback"
    ]


def test_github_callback_creates_session_without_persisting_access_token():
    class FakeGithubOAuthClient:
        def __init__(self) -> None:
            self.exchanges: list[dict[str, str]] = []

        async def authenticate(
            self,
            *,
            code: str,
            code_verifier: str,
            redirect_uri: str,
        ) -> dict[str, str]:
            self.exchanges.append(
                {
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": redirect_uri,
                }
            )
            return {"subject": "1234567", "login": "octo-cat"}

    oauth_client = FakeGithubOAuthClient()
    repository = MemoryConnectorRepository()
    config = ConnectorGatewayConfig(
        database_url="postgresql://unused-in-memory",
        session_secret="session-secret-that-is-more-than-32-characters",
        public_app_url="https://www.arena402.com",
        github_oauth_callback_base_url="https://api.arena402.com",
        github_oauth_client_id="github-client-id",
        github_oauth_client_secret="github-client-secret",
    )
    bundle = build_production_connector(
        config,
        repository,
        github_oauth_client=oauth_client,
    )
    app = FastAPI()
    app.include_router(bundle.router)
    client = TestClient(app, base_url="https://api.arena402.com")
    started = client.get(
        "/api/auth/github/start",
        params={"return_to": "/agents?tab=hosted"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]

    callback = client.get(
        "/api/auth/github/callback",
        params={"code": "temporary-code", "state": state},
        follow_redirects=False,
    )

    assert callback.status_code == 307
    assert callback.headers["cache-control"] == "no-store"
    assert callback.headers["location"] == (
        "https://www.arena402.com/founding402/claim"
    )
    assert oauth_client.exchanges == [
        {
            "code": "temporary-code",
            "code_verifier": oauth_client.exchanges[0]["code_verifier"],
            "redirect_uri": "https://api.arena402.com/api/auth/github/callback",
        }
    ]
    assert len(oauth_client.exchanges[0]["code_verifier"]) >= 43
    session = client.get("/api/auth/session")
    assert session.status_code == 200
    assert session.headers["cache-control"] == "no-store"
    assert session.json()["user"] == {
        "user_id": session.json()["user"]["user_id"],
        "username": "octo-cat",
        "temporary": False,
    }
    stored_user = repository.users[session.json()["user"]["user_id"]]
    assert stored_user["identity_provider"] == "github"
    assert stored_user["provider_subject"] == "1234567"
    assert "access_token" not in stored_user
    password_login = client.post(
        "/api/auth/login",
        json={"username": "octo-cat", "password": "correct horse battery staple"},
    )
    assert password_login.status_code == 401


def test_github_subject_not_mutable_login_owns_arena_identity():
    class RenamingGithubOAuthClient:
        def __init__(self) -> None:
            self.logins = iter(("octo-cat", "renamed-octo-cat"))

        async def authenticate(self, **_kwargs) -> dict[str, str]:
            return {"subject": "7654321", "login": next(self.logins)}

    invite = "github-collision-invite-that-is-long-enough"
    repository = MemoryConnectorRepository()
    config = ConnectorGatewayConfig(
        database_url="postgresql://unused-in-memory",
        session_secret="session-secret-that-is-more-than-32-characters",
        public_app_url="https://arena402.com",
        github_oauth_client_id="github-client-id",
        github_oauth_client_secret="github-client-secret",
        bootstrap_invite_hash=_hash(invite),
    )
    bundle = build_production_connector(
        config,
        repository,
        github_oauth_client=RenamingGithubOAuthClient(),
    )
    app = FastAPI()
    app.include_router(bundle.router)
    client = TestClient(app, base_url="https://arena402.com")
    local = client.post(
        "/api/auth/register",
        json={
            "invite_code": invite,
            "username": "octo-cat",
            "password": "correct horse battery staple",
        },
    ).json()["user"]
    client.cookies.clear()

    def github_sign_in() -> tuple[dict, str]:
        started = client.get(
            "/api/auth/github/start",
            follow_redirects=False,
        )
        state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        response = client.get(
            "/api/auth/github/callback",
            params={"code": "temporary-code", "state": state},
            follow_redirects=False,
        )
        assert response.status_code == 307
        return client.get("/api/auth/session").json()["user"], response.headers[
            "location"
        ]

    first, first_location = github_sign_in()
    assert first["user_id"] != local["user_id"]
    assert first["username"] == "github-7654321"
    assert first_location == "https://arena402.com/founding402/claim"
    client.cookies.clear()
    second, second_location = github_sign_in()
    assert second["user_id"] == first["user_id"]
    assert second["username"] == first["username"]
    assert second_location == "https://arena402.com/agents"


def test_github_callback_uses_frontend_error_contract_and_rejects_bad_state():
    config = ConnectorGatewayConfig(
        database_url="postgresql://unused-in-memory",
        session_secret="session-secret-that-is-more-than-32-characters",
        public_app_url="https://arena402.com",
        github_oauth_client_id="github-client-id",
        github_oauth_client_secret="github-client-secret",
    )
    bundle = build_production_connector(config, MemoryConnectorRepository())
    app = FastAPI()
    app.include_router(bundle.router)
    client = TestClient(app, base_url="https://arena402.com")

    started = client.get(
        "/api/auth/github/start",
        params={"return_to": "/connect"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    denied = client.get(
        "/api/auth/github/callback",
        params={"error": "access_denied", "state": state},
        follow_redirects=False,
    )
    denied_query = parse_qs(urlparse(denied.headers["location"]).query)
    assert denied_query == {
        "error": ["github_denied"],
        "return_to": ["/connect"],
    }
    assert denied.headers["cache-control"] == "no-store"

    tampered = client.get(
        "/api/auth/github/callback",
        params={"code": "temporary-code", "state": "attacker-state"},
        follow_redirects=False,
    )
    tampered_query = parse_qs(urlparse(tampered.headers["location"]).query)
    assert tampered_query == {
        "error": ["invalid_state"],
        "return_to": ["/agents"],
    }
    assert client.get("/api/auth/session").status_code == 401


def test_github_sign_in_unavailable_redirects_safely_without_open_redirect():
    config = ConnectorGatewayConfig(
        database_url="postgresql://unused-in-memory",
        session_secret="session-secret-that-is-more-than-32-characters",
        public_app_url="https://arena402.com",
    )
    bundle = build_production_connector(config, MemoryConnectorRepository())
    app = FastAPI()
    app.include_router(bundle.router)
    client = TestClient(app, base_url="https://arena402.com")

    response = client.get(
        "/api/auth/github/start",
        params={"return_to": "//attacker.example/path"},
        follow_redirects=False,
    )

    assert response.status_code == 307
    location = urlparse(response.headers["location"])
    assert (location.scheme, location.netloc, location.path) == (
        "https",
        "arena402.com",
        "/signin",
    )
    assert parse_qs(location.query) == {
        "error": ["github_unavailable"],
        "return_to": ["/agents"],
    }


def test_invite_is_one_time_password_is_argon2id_and_cookie_is_hardened():
    invite_code = "invite-register-that-is-long-enough"
    bundle, repository, client = _bundle(invite_code)
    response = client.post(
        "/api/auth/register",
        json={
            "invite_code": invite_code,
            "username": "Alice.Admin",
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 201
    assert response.json()["user"]["username"] == "alice.admin"
    assert repository.users[response.json()["user"]["user_id"]][
        "password_hash"
    ].startswith("$argon2id$")
    session_cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=lax" in session_cookie

    replay = TestClient(client.app, base_url="https://testserver").post(
        "/api/auth/register",
        json={
            "invite_code": invite_code,
            "username": "mallory",
            "password": "another long password",
        },
    )
    assert replay.status_code == 401

    fresh_client = TestClient(client.app, base_url="https://testserver")
    login = fresh_client.post(
        "/api/auth/login",
        json={
            "username": "ALICE.ADMIN",
            "password": "correct horse battery staple",
        },
    )
    assert login.status_code == 200
    wrong = fresh_client.post(
        "/api/auth/login",
        json={"username": "alice.admin", "password": "wrong"},
    )
    assert wrong.status_code == 401


def test_public_registration_creates_a_platform_account_without_an_invite():
    _, repository, client = _bundle(public_registration_enabled=True)

    response = client.post(
        "/api/auth/register",
        json={
            "username": "Public.Player",
            "password": "correct horse battery staple",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["user"]["username"] == "public.player"
    user_id = response.json()["user"]["user_id"]
    assert repository.users[user_id]["identity_provider"] == "password"
    assert repository.users[user_id]["provider_subject"] is None
    assert repository.users[user_id]["password_hash"].startswith("$argon2id$")


def test_invite_less_registration_fails_closed_by_default():
    _, _, client = _bundle()

    response = client.post(
        "/api/auth/register",
        json={
            "username": "public.player",
            "password": "correct horse battery staple",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Registration requires an invite"


def test_invite_creates_recoverable_account_and_requires_credentials():
    invite_code = "invite-recovery-that-is-long-enough"
    bundle, repository, client = _bundle(invite_code)
    missing_credentials = client.post(
        "/api/auth/invite",
        json={"invite_code": invite_code},
    )
    assert missing_credentials.status_code == 422

    accepted = client.post(
        "/api/auth/invite",
        json={
            "invite_code": invite_code,
            "username": "recoverable.user",
            "password": "correct horse battery staple",
        },
    )
    assert accepted.status_code == 201
    assert accepted.json()["user"]["temporary"] is False

    signed = client.cookies.get(bundle.config.session_cookie_name)
    raw_token = bundle.auth._signer.loads(signed)
    repository.sessions[_hash(raw_token)]["expires_at"] = datetime.now(
        timezone.utc
    ) - timedelta(seconds=1)
    assert client.get("/api/auth/session").status_code == 401

    fresh = TestClient(client.app, base_url="https://testserver")
    recovered = fresh.post(
        "/api/auth/login",
        json={
            "username": "recoverable.user",
            "password": "correct horse battery staple",
        },
    )
    assert recovered.status_code == 200
    assert recovered.json()["user"]["user_id"] == accepted.json()["user"]["user_id"]


def test_invalid_invite_skips_argon2_and_password_work_runs_off_event_loop():
    invite_code = "invite-argon-that-is-long-enough"
    bundle, _, _ = _bundle(invite_code)

    class RecordingHasher:
        def __init__(self):
            self.hash_threads: list[int] = []
            self.verify_threads: list[int] = []

        def hash(self, password: str) -> str:
            self.hash_threads.append(threading.get_ident())
            return "$argon2id$test"

        def verify(self, password_hash: str, password: str) -> bool:
            self.verify_threads.append(threading.get_ident())
            return password == "correct horse battery staple"

    hasher = RecordingHasher()
    bundle.auth._password_hasher = hasher

    async def scenario():
        event_loop_thread = threading.get_ident()
        with pytest.raises(Exception, match="Invalid or already used invite"):
            await bundle.auth.accept_invite(
                "invalid-invite-that-is-long-enough",
                "invalid.user",
                "correct horse battery staple",
            )
        assert hasher.hash_threads == []

        await bundle.auth.accept_invite(
            invite_code,
            "threaded.user",
            "correct horse battery staple",
        )
        assert hasher.hash_threads
        assert all(thread_id != event_loop_thread for thread_id in hasher.hash_threads)

        await bundle.auth.login(
            "threaded.user",
            "correct horse battery staple",
        )
        assert hasher.verify_threads
        assert all(
            thread_id != event_loop_thread for thread_id in hasher.verify_threads
        )

    asyncio.run(scenario())


def test_browser_control_requires_auth_and_csrf_and_ignores_forged_owner():
    invite_code = "invite-control-that-is-long-enough"
    bundle, _, client = _bundle(invite_code)
    pairing = _create_pairing(client)
    unauthenticated = client.get("/api/connectors/devices")
    assert unauthenticated.status_code == 401

    csrf = _invite(client, invite_code)
    missing_csrf = client.post(
        f"/api/connectors/pairings/{pairing['user_code']}/approve",
        json={"owner_id": "forged-owner"},
    )
    assert missing_csrf.status_code == 403
    approved = client.post(
        f"/api/connectors/pairings/{pairing['user_code']}/approve",
        json={"owner_id": "forged-owner"},
        headers={"X-CSRF-Token": csrf},
    )
    assert approved.status_code == 200
    assert approved.json()["owner_id"] != "forged-owner"

    approval_replay = client.post(
        f"/api/connectors/pairings/{pairing['user_code']}/approve",
        json={},
        headers={"X-CSRF-Token": csrf},
    )
    assert approval_replay.status_code == 409
    exchanged = client.post(
        "/api/connectors/pairings/exchange",
        json={"device_code": pairing["device_code"]},
    )
    assert exchanged.status_code == 200
    exchange_replay = client.post(
        "/api/connectors/pairings/exchange",
        json={"device_code": pairing["device_code"]},
    )
    assert exchange_replay.status_code == 401

    device = client.get(f"/api/connectors/devices/{exchanged.json()['device_id']}")
    assert device.status_code == 200
    assert device.json()["owner_id"] == approved.json()["owner_id"]


def test_expired_pairing_and_expired_or_revoked_sessions_are_rejected():
    invite_code = "invite-expiry-that-is-long-enough"
    bundle, repository, client = _bundle(invite_code)
    csrf = _invite(client, invite_code)
    pairing = _create_pairing(client)
    bundle.service.pairings[pairing["user_code"]]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    expired = client.post(
        f"/api/connectors/pairings/{pairing['user_code']}/approve",
        json={},
        headers={"X-CSRF-Token": csrf},
    )
    assert expired.status_code == 410

    signed = client.cookies.get(bundle.config.session_cookie_name)
    raw_token = bundle.auth._signer.loads(signed)
    repository.sessions[_hash(raw_token)]["expires_at"] = datetime.now(
        timezone.utc
    ) - timedelta(seconds=1)
    assert client.get("/api/connectors/devices").status_code == 401

    second_invite = "invite-logout-that-is-long-enough"
    repository.invites[_hash(second_invite)] = {
        "token_hash": _hash(second_invite),
        "expires_at": None,
        "consumed_at": None,
        "consumed_by": None,
    }
    second = TestClient(client.app, base_url="https://testserver")
    second_csrf = _invite(second, second_invite)
    logout = second.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": second_csrf},
    )
    assert logout.status_code == 204
    assert second.get("/api/connectors/devices").status_code == 401


def test_cross_tenant_device_binding_command_event_and_audit_are_hidden():
    owner_invite = "invite-owner-that-is-long-enough"
    bundle, repository, owner = _bundle(owner_invite)
    owner_csrf = _invite(owner, owner_invite)
    pairing = _create_pairing(owner)
    approved = owner.post(
        f"/api/connectors/pairings/{pairing['user_code']}/approve",
        json={},
        headers={"X-CSRF-Token": owner_csrf},
    )
    credential = owner.post(
        "/api/connectors/pairings/exchange",
        json={"device_code": pairing["device_code"]},
    ).json()
    asyncio.run(
        bundle.service.update_inventory(
            credential["device_id"],
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
        )
    )
    forged_binding = owner.post(
        f"/api/connectors/devices/{credential['device_id']}/bindings",
        json={"runtime_id": "codex-default", "agent_id": "agent-other-tenant"},
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert forged_binding.status_code == 422
    assert asyncio.run(bundle.service.list_bindings()) == []
    binding = owner.post(
        f"/api/connectors/devices/{credential['device_id']}/bindings",
        json={
            "runtime_id": "codex-default",
            "working_directory": "E:\\arena",
        },
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert binding.status_code == 201
    assert binding.json()["working_directory"] == "E:\\arena"
    binding_id = binding.json()["binding_id"]
    command = owner.post(
        f"/api/connectors/bindings/{binding_id}/commands",
        json={"action": "runtime.probe", "payload": {}},
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert command.status_code == 202

    intruder_invite = "invite-intruder-that-is-long-enough"
    repository.invites[_hash(intruder_invite)] = {
        "token_hash": _hash(intruder_invite),
        "expires_at": None,
        "consumed_at": None,
        "consumed_by": None,
    }
    intruder = TestClient(owner.app, base_url="https://testserver")
    intruder_csrf = _invite(intruder, intruder_invite)
    assert (
        intruder.get(f"/api/connectors/devices/{credential['device_id']}").status_code
        == 404
    )
    assert (
        intruder.get(f"/api/connectors/bindings/{binding_id}/commands").status_code
        == 404
    )
    assert (
        intruder.get(f"/api/connectors/bindings/{binding_id}/events").status_code == 404
    )
    assert (
        intruder.post(
            f"/api/connectors/bindings/{binding_id}/commands",
            json={"action": "runtime.probe", "payload": {}},
            headers={"X-CSRF-Token": intruder_csrf},
        ).status_code
        == 404
    )
    assert intruder.get("/api/connectors/audit").json()["total"] == 0
    assert owner.get("/api/connectors/audit").json()["total"] > 0


def test_revocation_invalidates_device_token_and_state_survives_restart():
    invite_code = "invite-durable-that-is-long-enough"
    bundle, repository, client = _bundle(invite_code)
    csrf = _invite(client, invite_code)
    pairing = _create_pairing(client)
    approved = client.post(
        f"/api/connectors/pairings/{pairing['user_code']}/approve",
        json={},
        headers={"X-CSRF-Token": csrf},
    ).json()
    credential = client.post(
        "/api/connectors/pairings/exchange",
        json={"device_code": pairing["device_code"]},
    ).json()

    restarted = PersistentConnectorGateway(
        repository,
        verification_uri="https://arena.example.test/connect",
    )

    async def restart_scenario():
        await restarted.initialize()
        restored = await restarted.list_devices(approved["owner_id"])
        assert restored[0]["device_id"] == credential["device_id"]
        await restarted.authenticate_device(
            credential["device_id"], credential["device_token"]
        )

    asyncio.run(restart_scenario())

    revoked = client.post(
        f"/api/connectors/devices/{credential['device_id']}/revoke",
        json={},
        headers={"X-CSRF-Token": csrf},
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    async def revoked_scenario():
        with pytest.raises(Exception, match="revoked"):
            await bundle.service.authenticate_device(
                credential["device_id"], credential["device_token"]
            )

    asyncio.run(revoked_scenario())


def test_shared_connection_lease_fences_the_replaced_gateway_instance():
    repository = MemoryConnectorRepository()
    first = PersistentConnectorGateway(
        repository,
        verification_uri="https://arena.example.test/connect",
        instance_id="gateway-a",
    )

    class Socket:
        def __init__(self):
            self.closed: list[tuple[int, str]] = []
            self.sent: list[dict] = []

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self, code: int, reason: str):
            self.closed.append((code, reason))

    async def scenario():
        await first.initialize()
        pairing = await first.create_pairing(None, "Lease laptop")
        await first.approve_pairing(pairing["user_code"], "owner-lease")
        credential = await first.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        await first.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
        )
        binding = await first.create_binding(
            device_id,
            "codex-default",
            None,
            None,
        )

        second = PersistentConnectorGateway(
            repository,
            verification_uri="https://arena.example.test/connect",
            instance_id="gateway-b",
        )
        await second.initialize()
        first_socket = Socket()
        second_socket = Socket()
        first_generation = await first.connect_device(device_id, first_socket)
        await first.mark_transport_ready(device_id, first_generation)
        await first.assert_active_connection(
            device_id,
            first_socket,
            first_generation,
        )

        second_generation = await second.connect_device(device_id, second_socket)
        await second.assert_active_connection(
            device_id,
            second_socket,
            second_generation,
        )
        with pytest.raises(ConnectorError, match="replaced"):
            await first.assert_active_connection(
                device_id,
                first_socket,
                first_generation,
            )
        stale_command = await first.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "stale-instance-probe",
            300,
        )
        assert stale_command["status"] == "queued"
        assert first_socket.sent == []

        await first.disconnect_device(device_id, first_socket)
        await second.assert_active_connection(
            device_id,
            second_socket,
            second_generation,
        )
        persisted = await repository.load_gateway_state()
        persisted_device = next(
            device for device in persisted["devices"] if device["device_id"] == device_id
        )
        assert persisted_device["status"] == "online"

    asyncio.run(scenario())


def test_stale_disconnect_cannot_persist_offline_during_a_new_claim():
    class TakeoverOnReleaseRepository(MemoryConnectorRepository):
        def __init__(self):
            super().__init__()
            self.before_release = None

        async def release_device_connection_and_save_device(
            self,
            device_id: str,
            instance_id: str,
            fencing_token: int,
            device: dict,
        ) -> bool:
            callback = self.before_release
            self.before_release = None
            if callback is not None:
                await callback()
            return await super().release_device_connection_and_save_device(
                device_id,
                instance_id,
                fencing_token,
                device,
            )

    repository = TakeoverOnReleaseRepository()
    first = PersistentConnectorGateway(repository, instance_id="gateway-old")

    class Socket:
        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await first.initialize()
        pairing = await first.create_pairing(None, "Race laptop")
        await first.approve_pairing(pairing["user_code"], "owner-race")
        credential = await first.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        second = PersistentConnectorGateway(repository, instance_id="gateway-new")
        await second.initialize()
        old_socket = Socket()
        new_socket = Socket()
        await first.connect_device(device_id, old_socket)

        async def claim_from_new_gateway():
            await second.connect_device(device_id, new_socket)

        repository.before_release = claim_from_new_gateway
        await first.disconnect_device(device_id, old_socket)

        persisted = await repository.load_gateway_state()
        persisted_device = next(
            item for item in persisted["devices"] if item["device_id"] == device_id
        )
        assert persisted_device["status"] == "online"
        await second.assert_active_connection(device_id, new_socket)

    asyncio.run(scenario())


def test_gateway_close_releases_its_owned_connection_lease():
    repository = MemoryConnectorRepository()
    service = PersistentConnectorGateway(
        repository,
        instance_id="gateway-close",
    )

    class Socket:
        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await service.initialize()
        pairing = await service.create_pairing(None, "Close laptop")
        await service.approve_pairing(pairing["user_code"], "owner-close")
        credential = await service.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        socket = Socket()
        await service.connect_device(device_id, socket)
        fencing_token = int(
            repository.connection_leases[device_id]["fencing_token"]
        )
        assert await repository.is_device_connection_owner(
            device_id,
            "gateway-close",
            fencing_token,
        )

        await service.close()

        assert not await repository.is_device_connection_owner(
            device_id,
            "gateway-close",
            fencing_token,
        )
        persisted = await repository.load_gateway_state()
        persisted_device = next(
            item for item in persisted["devices"] if item["device_id"] == device_id
        )
        assert persisted_device["status"] == "offline"

    asyncio.run(scenario())


def test_connection_claim_failure_rolls_back_the_local_socket_owner():
    class FailingClaimRepository(MemoryConnectorRepository):
        async def claim_device_connection(
            self,
            device_id: str,
            instance_id: str,
            lease_seconds: int,
        ) -> int:
            raise RuntimeError("lease database unavailable")

    repository = FailingClaimRepository()
    service = PersistentConnectorGateway(repository, instance_id="gateway-fail")

    class Socket:
        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await service.initialize()
        pairing = await service.create_pairing(None, "Fail laptop")
        await service.approve_pairing(pairing["user_code"], "owner-fail")
        credential = await service.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        socket = Socket()

        with pytest.raises(RuntimeError, match="lease database unavailable"):
            await service.connect_device(device_id, socket)

        assert device_id not in service.connections
        assert service.devices[device_id]["status"] == "offline"

    asyncio.run(scenario())


def test_connection_persistence_failure_releases_the_claimed_lease():
    class FailingPersistRepository(MemoryConnectorRepository):
        def __init__(self):
            super().__init__()
            self.fail_online_save = False

        async def save_gateway_state(self, state):
            if self.fail_online_save and any(
                device.get("status") == "online" for device in state.get("devices", [])
            ):
                raise RuntimeError("connection persistence unavailable")
            await super().save_gateway_state(state)

    repository = FailingPersistRepository()
    service = PersistentConnectorGateway(
        repository,
        instance_id="gateway-persist-fail",
    )

    class Socket:
        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await service.initialize()
        pairing = await service.create_pairing(None, "Persist fail laptop")
        await service.approve_pairing(pairing["user_code"], "owner-persist-fail")
        credential = await service.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        socket = Socket()
        repository.fail_online_save = True

        with pytest.raises(RuntimeError, match="connection persistence unavailable"):
            await service.connect_device(device_id, socket)

        lease = repository.connection_leases[device_id]
        assert not await repository.is_device_connection_owner(
            device_id,
            "gateway-persist-fail",
            int(lease["fencing_token"]),
        )
        assert device_id not in service.connections

    asyncio.run(scenario())


def test_standby_startup_does_not_recover_a_device_with_a_live_owner():
    repository = MemoryConnectorRepository()
    owner = PersistentConnectorGateway(repository, instance_id="gateway-owner")

    class Socket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await owner.initialize()
        pairing = await owner.create_pairing(None, "Standby laptop")
        await owner.approve_pairing(pairing["user_code"], "owner-standby")
        credential = await owner.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        await owner.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
        )
        binding = await owner.create_binding(
            device_id,
            "codex-default",
            None,
            None,
        )
        socket = Socket()
        generation = await owner.connect_device(device_id, socket)
        await owner.mark_transport_ready(device_id, generation)
        command = await owner.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "standby-probe",
            300,
        )
        assert command["status"] == "delivered"

        standby = PersistentConnectorGateway(
            repository,
            instance_id="gateway-standby",
        )
        await standby.initialize()

        persisted = await repository.load_gateway_state()
        persisted_device = next(
            device for device in persisted["devices"] if device["device_id"] == device_id
        )
        persisted_command = next(
            item
            for item in persisted["commands"]
            if item["command_id"] == command["command_id"]
        )
        assert persisted_device["status"] == "online"
        assert persisted_command["status"] == "delivered"
        await owner.assert_active_connection(device_id, socket, generation)

    asyncio.run(scenario())


def test_wss_owner_routes_a_command_queued_on_another_gateway_instance():
    repository = MemoryConnectorRepository()
    ingress = PersistentConnectorGateway(repository, instance_id="gateway-ingress")

    class Socket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await ingress.initialize()
        pairing = await ingress.create_pairing(None, "Routed laptop")
        await ingress.approve_pairing(pairing["user_code"], "owner-routed")
        credential = await ingress.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        await ingress.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
        )
        binding = await ingress.create_binding(
            device_id,
            "codex-default",
            None,
            None,
        )
        owner = PersistentConnectorGateway(repository, instance_id="gateway-owner")
        await owner.initialize()
        socket = Socket()
        generation = await owner.connect_device(device_id, socket)
        await owner.mark_transport_ready(device_id, generation)

        queued = await ingress.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "cross-instance-probe",
            300,
        )
        assert queued["status"] == "queued"
        assert socket.sent == []

        assert await owner.route_shared_commands_once() == 1
        assert len(socket.sent) == 1
        assert socket.sent[0]["message_id"] == queued["command_id"]
        routed = await owner.list_commands(binding["binding_id"])
        assert next(
            item for item in routed if item["command_id"] == queued["command_id"]
        )["status"] == "delivered"

        assert await owner.route_shared_commands_once() == 0
        assert len(socket.sent) == 1

    asyncio.run(scenario())


def test_cross_instance_idempotent_replay_cannot_regress_delivered_command():
    repository = MemoryConnectorRepository()
    ingress = PersistentConnectorGateway(repository, instance_id="gateway-ingress")

    class Socket:
        async def send_json(self, payload):
            return None

        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await ingress.initialize()
        pairing = await ingress.create_pairing(None, "Monotonic laptop")
        await ingress.approve_pairing(pairing["user_code"], "owner-monotonic")
        credential = await ingress.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        await ingress.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
        )
        binding = await ingress.create_binding(
            device_id,
            "codex-default",
            None,
            None,
        )
        owner = PersistentConnectorGateway(repository, instance_id="gateway-owner")
        await owner.initialize()
        socket = Socket()
        generation = await owner.connect_device(device_id, socket)
        await owner.mark_transport_ready(device_id, generation)
        created = await ingress.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "monotonic-probe",
            300,
        )
        assert await owner.route_shared_commands_once() == 1

        replay = await ingress.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "monotonic-probe",
            300,
        )
        assert replay["command_id"] == created["command_id"]

        observer = PersistentConnectorGateway(
            repository,
            instance_id="gateway-observer",
        )
        await observer.initialize()
        observed = await observer.list_commands(binding["binding_id"])
        assert next(
            item for item in observed if item["command_id"] == created["command_id"]
        )["status"] == "delivered"

    asyncio.run(scenario())


def test_shared_command_route_hydrates_a_binding_created_after_owner_startup():
    repository = MemoryConnectorRepository()
    ingress = PersistentConnectorGateway(repository, instance_id="gateway-ingress")

    class Socket:
        async def send_json(self, payload):
            return None

        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await ingress.initialize()
        pairing = await ingress.create_pairing(None, "Late binding laptop")
        await ingress.approve_pairing(pairing["user_code"], "owner-late-binding")
        credential = await ingress.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        await ingress.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
        )
        owner = PersistentConnectorGateway(repository, instance_id="gateway-owner")
        await owner.initialize()
        socket = Socket()
        generation = await owner.connect_device(device_id, socket)
        await owner.mark_transport_ready(device_id, generation)

        binding = await ingress.create_binding(
            device_id,
            "codex-default",
            None,
            None,
        )
        await ingress.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "late-binding-probe",
            300,
        )

        assert await owner.route_shared_commands_once() == 1
        owner_bindings = await owner.list_bindings(device_id)
        assert any(
            item["binding_id"] == binding["binding_id"] for item in owner_bindings
        )

    asyncio.run(scenario())


def test_new_wss_owner_replays_a_shared_delivered_command_after_takeover():
    repository = MemoryConnectorRepository()
    ingress = PersistentConnectorGateway(repository, instance_id="gateway-ingress")

    class Socket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await ingress.initialize()
        pairing = await ingress.create_pairing(None, "Takeover laptop")
        await ingress.approve_pairing(pairing["user_code"], "owner-takeover")
        credential = await ingress.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        await ingress.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
        )
        binding = await ingress.create_binding(
            device_id,
            "codex-default",
            None,
            None,
        )
        old_owner = PersistentConnectorGateway(repository, instance_id="gateway-old")
        new_owner = PersistentConnectorGateway(repository, instance_id="gateway-new")
        await old_owner.initialize()
        await new_owner.initialize()
        old_socket = Socket()
        old_generation = await old_owner.connect_device(device_id, old_socket)
        await old_owner.mark_transport_ready(device_id, old_generation)
        command = await ingress.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "takeover-probe",
            300,
        )
        assert await old_owner.route_shared_commands_once() == 1
        assert old_socket.sent[0]["message_id"] == command["command_id"]

        new_socket = Socket()
        new_generation = await new_owner.connect_device(device_id, new_socket)
        await new_owner.mark_transport_ready(device_id, new_generation)
        assert await new_owner.route_shared_commands_once() == 1
        assert len(new_socket.sent) == 1
        assert new_socket.sent[0]["message_id"] == command["command_id"]

    asyncio.run(scenario())


def test_takeover_between_socket_write_and_delivery_commit_keeps_replay_queued():
    class TakeoverBeforeDeliveryCommitRepository(MemoryConnectorRepository):
        def __init__(self):
            super().__init__()
            self.before_delivery_commit = None

        async def save_command_for_connection_owner(
            self,
            device_id: str,
            instance_id: str,
            fencing_token: int,
            command: dict,
        ) -> bool:
            callback = self.before_delivery_commit
            self.before_delivery_commit = None
            if callback is not None:
                await callback()
            return await super().save_command_for_connection_owner(
                device_id,
                instance_id,
                fencing_token,
                command,
            )

    repository = TakeoverBeforeDeliveryCommitRepository()
    ingress = PersistentConnectorGateway(repository, instance_id="gateway-ingress")

    class Socket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await ingress.initialize()
        pairing = await ingress.create_pairing(None, "Commit race laptop")
        await ingress.approve_pairing(pairing["user_code"], "owner-commit-race")
        credential = await ingress.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        await ingress.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
        )
        binding = await ingress.create_binding(
            device_id,
            "codex-default",
            None,
            None,
        )
        old_owner = PersistentConnectorGateway(repository, instance_id="gateway-old")
        new_owner = PersistentConnectorGateway(repository, instance_id="gateway-new")
        await old_owner.initialize()
        await new_owner.initialize()
        old_socket = Socket()
        old_generation = await old_owner.connect_device(device_id, old_socket)
        await old_owner.mark_transport_ready(device_id, old_generation)
        command = await ingress.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "commit-race-probe",
            300,
        )
        new_socket = Socket()

        async def takeover():
            generation = await new_owner.connect_device(device_id, new_socket)
            await new_owner.mark_transport_ready(device_id, generation)

        repository.before_delivery_commit = takeover
        assert await old_owner.route_shared_commands_once() == 1
        assert old_socket.sent[0]["message_id"] == command["command_id"]
        assert await new_owner.route_shared_commands_once() == 1
        assert new_socket.sent[0]["message_id"] == command["command_id"]

    asyncio.run(scenario())


def test_terminal_ack_wins_over_a_late_shared_delivery_commit():
    class AckBeforeDeliveryCommitRepository(MemoryConnectorRepository):
        def __init__(self):
            super().__init__()
            self.before_delivery_commit = None

        async def save_command_for_connection_owner(
            self,
            device_id: str,
            instance_id: str,
            fencing_token: int,
            command: dict,
        ) -> bool:
            callback = self.before_delivery_commit
            self.before_delivery_commit = None
            if callback is not None:
                await callback()
            return await super().save_command_for_connection_owner(
                device_id,
                instance_id,
                fencing_token,
                command,
            )

    repository = AckBeforeDeliveryCommitRepository()
    ingress = PersistentConnectorGateway(repository, instance_id="gateway-ingress")

    class Socket:
        async def send_json(self, payload):
            return None

        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await ingress.initialize()
        pairing = await ingress.create_pairing(None, "ACK race laptop")
        await ingress.approve_pairing(pairing["user_code"], "owner-ack-race")
        credential = await ingress.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        await ingress.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
        )
        binding = await ingress.create_binding(
            device_id,
            "codex-default",
            None,
            None,
        )
        owner = PersistentConnectorGateway(repository, instance_id="gateway-owner")
        await owner.initialize()
        socket = Socket()
        generation = await owner.connect_device(device_id, socket)
        await owner.mark_transport_ready(device_id, generation)
        command = await ingress.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "ack-race-probe",
            300,
        )

        async def acknowledge():
            await owner.acknowledge_command(
                device_id,
                {
                    "command_id": command["command_id"],
                    "status": "succeeded",
                    "result": {"available": True},
                },
                generation,
            )

        repository.before_delivery_commit = acknowledge
        assert await owner.route_shared_commands_once() == 1
        observer = PersistentConnectorGateway(repository, instance_id="gateway-observer")
        await observer.initialize()
        observed = await observer.list_commands(binding["binding_id"])
        assert next(
            item for item in observed if item["command_id"] == command["command_id"]
        )["status"] == "succeeded"

    asyncio.run(scenario())


def test_new_owner_inherits_the_shared_outbound_sequence_before_resume():
    repository = MemoryConnectorRepository()
    ingress = PersistentConnectorGateway(repository, instance_id="gateway-ingress")

    class Socket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await ingress.initialize()
        pairing = await ingress.create_pairing(None, "Resume takeover laptop")
        await ingress.approve_pairing(pairing["user_code"], "owner-resume-takeover")
        credential = await ingress.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        await ingress.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
        )
        binding = await ingress.create_binding(
            device_id,
            "codex-default",
            None,
            None,
        )
        old_owner = PersistentConnectorGateway(repository, instance_id="gateway-old")
        new_owner = PersistentConnectorGateway(repository, instance_id="gateway-new")
        await old_owner.initialize()
        await new_owner.initialize()
        old_socket = Socket()
        old_generation = await old_owner.connect_device(device_id, old_socket)
        await old_owner.mark_transport_ready(device_id, old_generation)
        await ingress.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "resume-takeover-probe",
            300,
        )
        assert await old_owner.route_shared_commands_once() == 1
        observed_sequence = old_socket.sent[0]["sequence"]

        new_socket = Socket()
        new_generation = await new_owner.connect_device(device_id, new_socket)
        resumed = await new_owner.resume_transport(
            device_id,
            {
                "last_gateway_sequence": observed_sequence,
                "event_ack_through": 0,
                "pending_result_ids": [],
            },
            new_generation,
        )
        assert resumed["gateway_sequence"] >= observed_sequence

    asyncio.run(scenario())


def test_wss_worker_refreshes_a_device_enrolled_after_worker_startup():
    repository = MemoryConnectorRepository()
    control = PersistentConnectorGateway(repository, instance_id="gateway-control")
    wss_worker = PersistentConnectorGateway(repository, instance_id="gateway-wss")

    class Socket:
        async def send_json(self, payload):
            return None

        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await wss_worker.initialize()
        await control.initialize()
        pairing = await control.create_pairing(None, "Late enrolled laptop")
        await control.approve_pairing(pairing["user_code"], "owner-late-device")
        credential = await control.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        await control.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
        )
        binding = await control.create_binding(
            device_id,
            "codex-default",
            None,
            None,
        )

        authenticated = await wss_worker.authenticate_device(
            device_id,
            credential["device_token"],
        )
        assert authenticated["device_id"] == device_id
        generation = await wss_worker.connect_device(device_id, Socket())
        assert generation > 0
        assert [
            item["binding_id"]
            for item in await wss_worker.list_bindings(device_id)
        ] == [binding["binding_id"]]

    asyncio.run(scenario())


def test_control_plane_revoke_cannot_be_undone_by_stale_wss_disconnect():
    repository = MemoryConnectorRepository()
    control = PersistentConnectorGateway(repository, instance_id="gateway-control")

    class Socket:
        async def send_json(self, payload):
            return None

        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await control.initialize()
        pairing = await control.create_pairing(None, "Revoked laptop")
        await control.approve_pairing(pairing["user_code"], "owner-revoked-device")
        credential = await control.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]

        wss_worker = PersistentConnectorGateway(repository, instance_id="gateway-wss")
        await wss_worker.initialize()
        socket = Socket()
        await wss_worker.connect_device(device_id, socket)

        revoked = await control.revoke_device(device_id, "owner-revoked-device")
        assert revoked["status"] == "revoked"
        await wss_worker.disconnect_device(device_id, socket)

        observer = PersistentConnectorGateway(
            repository,
            instance_id="gateway-observer",
        )
        await observer.initialize()
        observed = await observer.get_device(device_id)
        assert observed["status"] == "revoked"
        assert observed["revoked_at"] == revoked["revoked_at"]

    asyncio.run(scenario())


def test_takeover_fences_a_late_command_ack_commit():
    repository = MemoryConnectorRepository()
    control = PersistentConnectorGateway(repository, instance_id="gateway-control")

    class Socket:
        async def send_json(self, payload):
            return None

        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await control.initialize()
        pairing = await control.create_pairing(None, "ACK takeover laptop")
        await control.approve_pairing(pairing["user_code"], "owner-ack-takeover")
        credential = await control.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        await control.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
        )
        binding = await control.create_binding(
            device_id,
            "codex-default",
            None,
            None,
        )
        old_owner = PersistentConnectorGateway(repository, instance_id="gateway-old")
        new_owner = PersistentConnectorGateway(repository, instance_id="gateway-new")
        await old_owner.initialize()
        await new_owner.initialize()
        old_socket = Socket()
        old_generation = await old_owner.connect_device(device_id, old_socket)
        await old_owner.mark_transport_ready(device_id, old_generation)
        command = await control.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "late-ack-probe",
            300,
        )
        assert await old_owner.route_shared_commands_once() == 1

        await new_owner.connect_device(device_id, Socket())
        with pytest.raises(ConnectionReplacedError):
            await old_owner.acknowledge_command(
                device_id,
                {
                    "command_id": command["command_id"],
                    "status": "succeeded",
                    "result": {"available": True},
                },
                old_generation,
            )

        observer = PersistentConnectorGateway(
            repository,
            instance_id="gateway-observer",
        )
        await observer.initialize()
        observed = await observer.list_commands(binding["binding_id"])
        assert next(
            item for item in observed if item["command_id"] == command["command_id"]
        )["status"] == "queued"

    asyncio.run(scenario())


def test_control_plane_observes_binding_lifecycle_written_by_wss_worker():
    repository = MemoryConnectorRepository()
    control = PersistentConnectorGateway(repository, instance_id="gateway-control")

    class Socket:
        async def send_json(self, payload):
            return None

        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await control.initialize()
        pairing = await control.create_pairing(None, "Binding refresh laptop")
        await control.approve_pairing(pairing["user_code"], "owner-binding-refresh")
        credential = await control.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        await control.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=["session.start"],
                )
            ],
        )
        binding = await control.create_binding(
            device_id,
            "codex-default",
            None,
            None,
        )
        command = await control.queue_command(
            binding["binding_id"],
            CommandAction.SESSION_START,
            {"working_directory": "C:/arena"},
            "binding-refresh-start",
            300,
        )

        wss_worker = PersistentConnectorGateway(repository, instance_id="gateway-wss")
        await wss_worker.initialize()
        socket = Socket()
        generation = await wss_worker.connect_device(device_id, socket)
        await wss_worker.mark_transport_ready(device_id, generation)
        assert await wss_worker.route_shared_commands_once() == 1
        await wss_worker.acknowledge_command(
            device_id,
            {
                "command_id": command["command_id"],
                "status": "succeeded",
                "result": {"session_id": "session-shared"},
            },
            generation,
        )

        observed = next(
            item
            for item in await control.list_bindings(device_id)
            if item["binding_id"] == binding["binding_id"]
        )
        assert observed["status"] == "running"
        assert observed["last_session_id"] == "session-shared"
        audit = await control.list_audit(owner_id="owner-binding-refresh")
        assert any(
            item["action"] == "command.acknowledged"
            and item["metadata"]["command_id"] == command["command_id"]
            for item in audit
        )

    asyncio.run(scenario())


def test_control_plane_can_bind_runtime_inventory_written_by_wss_worker():
    repository = MemoryConnectorRepository()
    control = PersistentConnectorGateway(repository, instance_id="gateway-control")

    class Socket:
        async def send_json(self, payload):
            return None

        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await control.initialize()
        pairing = await control.create_pairing(None, "Inventory refresh laptop")
        await control.approve_pairing(pairing["user_code"], "owner-inventory-refresh")
        credential = await control.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]

        worker = PersistentConnectorGateway(repository, instance_id="gateway-wss")
        await worker.initialize()
        socket = Socket()
        generation = await worker.connect_device(device_id, socket)
        await worker.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
            expected_generation=generation,
        )

        binding = await control.create_binding(
            device_id,
            "codex-default",
            None,
            None,
        )
        assert binding["runtime_id"] == "codex-default"
        observed_device = next(
            item
            for item in await control.list_devices("owner-inventory-refresh")
            if item["device_id"] == device_id
        )
        assert observed_device["status"] == "online"
        assert (await control.get_device(device_id))["status"] == "online"

    asyncio.run(scenario())


def test_control_plane_can_revoke_a_device_enrolled_after_startup():
    repository = MemoryConnectorRepository()
    control = PersistentConnectorGateway(repository, instance_id="gateway-control")
    enrollment = PersistentConnectorGateway(
        repository,
        instance_id="gateway-enrollment",
    )

    async def scenario():
        await control.initialize()
        await enrollment.initialize()
        pairing = await enrollment.create_pairing(None, "Late revoke laptop")
        await enrollment.approve_pairing(pairing["user_code"], "owner-late-revoke")
        credential = await enrollment.exchange_pairing(pairing["device_code"])

        revoked = await control.revoke_device(
            credential["device_id"],
            "owner-late-revoke",
        )
        assert revoked["status"] == "revoked"

    asyncio.run(scenario())


def test_wss_worker_drain_closes_connections_and_rejects_new_ones():
    repository = MemoryConnectorRepository()
    control = PersistentConnectorGateway(repository, instance_id="gateway-control")

    class Socket:
        def __init__(self):
            self.closed: list[tuple[int, str]] = []

        async def send_json(self, payload):
            return None

        async def close(self, code: int, reason: str):
            self.closed.append((code, reason))

    async def scenario():
        await control.initialize()
        pairing = await control.create_pairing(None, "Drain laptop")
        await control.approve_pairing(pairing["user_code"], "owner-drain")
        credential = await control.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]

        worker = PersistentConnectorGateway(repository, instance_id="gateway-wss")
        await worker.initialize()
        socket = Socket()
        await worker.connect_device(device_id, socket)
        assert await repository.has_active_device_connection(device_id)

        await worker.begin_drain()
        assert socket.closed == [(1012, "Gateway worker restarting")]
        assert not await repository.has_active_device_connection(device_id)
        with pytest.raises(ConnectorError) as rejected:
            await worker.connect_device(device_id, Socket())
        assert rejected.value.status_code == 503

    asyncio.run(scenario())


def test_revoked_device_cannot_receive_a_command_from_background_router():
    repository = MemoryConnectorRepository()
    control = PersistentConnectorGateway(repository, instance_id="gateway-control")

    class Socket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await control.initialize()
        pairing = await control.create_pairing(None, "Revoked route laptop")
        await control.approve_pairing(pairing["user_code"], "owner-revoked-route")
        credential = await control.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        await control.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
        )
        binding = await control.create_binding(
            device_id,
            "codex-default",
            None,
            None,
        )
        worker = PersistentConnectorGateway(repository, instance_id="gateway-wss")
        await worker.initialize()
        socket = Socket()
        generation = await worker.connect_device(device_id, socket)
        await worker.mark_transport_ready(device_id, generation)
        await control.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "revoked-route-probe",
            300,
        )
        await control.revoke_device(device_id, "owner-revoked-route")

        assert await worker.route_shared_commands_once() == 0
        assert socket.sent == []

    asyncio.run(scenario())


def test_delivery_commit_failure_keeps_shared_command_replayable():
    class FailOnceDeliveryCommitRepository(MemoryConnectorRepository):
        def __init__(self):
            super().__init__()
            self.fail_next_delivery_commit = True

        async def save_command_for_connection_owner(
            self,
            device_id: str,
            instance_id: str,
            fencing_token: int,
            command: dict,
        ) -> bool:
            if self.fail_next_delivery_commit:
                self.fail_next_delivery_commit = False
                raise RuntimeError("delivery commit database unavailable")
            return await super().save_command_for_connection_owner(
                device_id,
                instance_id,
                fencing_token,
                command,
            )

    repository = FailOnceDeliveryCommitRepository()
    ingress = PersistentConnectorGateway(repository, instance_id="gateway-ingress")

    class Socket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self, code: int, reason: str):
            return None

    async def scenario():
        await ingress.initialize()
        pairing = await ingress.create_pairing(None, "Commit retry laptop")
        await ingress.approve_pairing(pairing["user_code"], "owner-commit-retry")
        credential = await ingress.exchange_pairing(pairing["device_code"])
        device_id = credential["device_id"]
        await ingress.update_inventory(
            device_id,
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
        )
        binding = await ingress.create_binding(
            device_id,
            "codex-default",
            None,
            None,
        )
        owner = PersistentConnectorGateway(repository, instance_id="gateway-owner")
        await owner.initialize()
        socket = Socket()
        generation = await owner.connect_device(device_id, socket)
        await owner.mark_transport_ready(device_id, generation)
        command = await ingress.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "commit-retry-probe",
            300,
        )

        with pytest.raises(RuntimeError, match="database unavailable"):
            await owner.route_shared_commands_once()
        assert socket.sent[0]["message_id"] == command["command_id"]
        assert await owner.route_shared_commands_once() == 1
        assert [frame["message_id"] for frame in socket.sent] == [
            command["command_id"],
            command["command_id"],
        ]

    asyncio.run(scenario())


def test_public_auth_and_pairing_ingress_are_rate_limited():
    bundle, _, client = _bundle(
        "invite-rate-limit-that-is-long-enough",
        auth_rate_limit_attempts=2,
        pairing_rate_limit_attempts=2,
    )

    class RejectingHasher:
        def verify(self, password_hash: str, password: str) -> bool:
            return False

    bundle.auth._password_hasher = RejectingHasher()
    for _ in range(2):
        response = client.post(
            "/api/auth/login",
            json={"username": "missing.user", "password": "wrong"},
        )
        assert response.status_code == 401
    limited_auth = client.post(
        "/api/auth/login",
        json={"username": "missing.user", "password": "wrong"},
    )
    assert limited_auth.status_code == 429
    assert int(limited_auth.headers["retry-after"]) >= 1

    for _ in range(2):
        assert _create_pairing(client)["status"] == "pending"
    limited_pairing = client.post(
        "/api/connectors/pairings",
        json={"device_name": "One too many"},
    )
    assert limited_pairing.status_code == 429


def test_pending_pairing_cap_and_expiry_cleanup_bound_persistent_state():
    bundle, repository, client = _bundle(
        "invite-cap-that-is-long-enough",
        max_pending_pairings=1,
    )
    first = _create_pairing(client)
    full = client.post(
        "/api/connectors/pairings",
        json={"device_name": "At capacity"},
    )
    assert full.status_code == 503

    # Expiry is refreshed synchronously by create_pairing before capacity is
    # evaluated, so stale unauthenticated records cannot hold the cap forever.
    bundle.service.pairings[first["user_code"]]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    replacement = _create_pairing(client)
    assert replacement["pairing_id"] != first["pairing_id"]
    assert list(bundle.service.pairings) == [replacement["user_code"]]
    assert [item["pairing_id"] for item in repository.gateway_state["pairings"]] == [
        replacement["pairing_id"]
    ]


def test_command_is_persisted_before_websocket_delivery_and_retry_is_idempotent():
    class OrderingRepository(MemoryConnectorRepository):
        def __init__(self):
            super().__init__()
            self.fail_next_command_save = False

        async def save_gateway_state(self, state):
            if self.fail_next_command_save and state["commands"]:
                self.fail_next_command_save = False
                raise RuntimeError("simulated database outage")
            await super().save_gateway_state(state)

    class RecordingSocket:
        def __init__(self, repository):
            self.repository = repository
            self.frames: list[dict] = []

        async def send_json(self, frame):
            command_id = frame["message_id"]
            persisted_ids = {
                command["command_id"]
                for command in self.repository.gateway_state["commands"]
            }
            assert command_id in persisted_ids
            persisted_device = next(
                device
                for device in self.repository.gateway_state["devices"]
                if device["device_id"] == frame["device_id"]
            )
            assert persisted_device["outbound_sequence"] >= frame["sequence"]
            self.frames.append(frame)

    invite_code = "invite-command-order-that-is-long-enough"
    repository = OrderingRepository()
    bundle, _, _ = _bundle(invite_code, repository=repository)

    async def scenario():
        issued = await bundle.auth.accept_invite(
            invite_code,
            "command.owner",
            "correct horse battery staple",
        )
        pairing = await bundle.service.create_pairing(None, "Command laptop")
        await bundle.service.approve_pairing(
            pairing["user_code"],
            issued.principal.user_id,
        )
        credential = await bundle.service.exchange_pairing(pairing["device_code"])
        await bundle.service.update_inventory(
            credential["device_id"],
            [
                RuntimeInventoryItem(
                    runtime_id="codex-default",
                    kind="codex",
                    display_name="Codex",
                    executable_path="codex",
                    capabilities=[],
                )
            ],
        )
        binding = await bundle.service.create_binding(
            credential["device_id"],
            "codex-default",
            None,
            None,
        )
        socket = RecordingSocket(repository)
        generation = await bundle.service.connect_device(
            credential["device_id"], socket
        )
        await bundle.service.mark_transport_ready(
            credential["device_id"], generation
        )

        repository.fail_next_command_save = True
        with pytest.raises(RuntimeError, match="database outage"):
            await bundle.service.queue_command(
                binding["binding_id"],
                CommandAction.RUNTIME_PROBE,
                {},
                "probe-once",
                300,
            )
        assert socket.frames == []

        command = await bundle.service.queue_command(
            binding["binding_id"],
            CommandAction.RUNTIME_PROBE,
            {},
            "probe-once",
            300,
        )
        assert command["status"] == "delivered"
        assert len(socket.frames) == 1
        assert len(repository.gateway_state["commands"]) == 1

    asyncio.run(scenario())


def test_observability_streams_are_persisted_as_deltas_and_survive_restart():
    class RecordingRepository(MemoryConnectorRepository):
        def __init__(self):
            super().__init__()
            self.observability_batches: list[tuple[int, int]] = []

        async def save_gateway_state(self, state):
            self.observability_batches.append(
                (len(state["events"]), len(state["audit"]))
            )
            await super().save_gateway_state(state)

    repository = RecordingRepository()
    service = PersistentConnectorGateway(
        repository,
        verification_uri="https://arena.example.test/connect",
    )

    async def scenario():
        await service.initialize()
        async with service._lock:
            service.devices["device_incremental"] = {
                "device_id": "device_incremental",
                "owner_id": "owner_incremental",
                "token_hash": "0" * 64,
                "status": "offline",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "revoked_at": None,
                "_connection_generation": 0,
                "event_ack_watermark": 1,
                "event_pending_sequences": [],
                "runtimes": [],
            }
            service.events.append(
                {
                    "event_id": "event_incremental",
                    "device_id": "device_incremental",
                    "binding_id": "binding_incremental",
                    "sequence": 1,
                    "event_type": "runtime.status",
                    "received_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            service.audit.append(
                {
                    "audit_id": "audit_incremental",
                    "owner_id": None,
                    "action": "test.incremental",
                    "actor": "test",
                    "metadata": {},
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        await service._persist_current()
        await service._persist_current()

        assert repository.observability_batches[-2:] == [(1, 1), (0, 0)]
        assert len(repository.gateway_state["events"]) == 1
        assert len(repository.gateway_state["audit"]) == 1

        restarted = PersistentConnectorGateway(
            repository,
            verification_uri="https://arena.example.test/connect",
        )
        await restarted.initialize()
        assert restarted.events[0]["event_id"] == "event_incremental"
        assert restarted.audit[0]["audit_id"] == "audit_incremental"
        assert restarted.event_ack_watermarks["device_incremental"] == 1

    asyncio.run(scenario())


def test_heartbeat_persists_only_the_touched_device_without_runtime_rewrite():
    class RecordingRepository(MemoryConnectorRepository):
        def __init__(self):
            super().__init__()
            self.states: list[dict] = []

        async def save_gateway_state(self, state):
            self.states.append(copy.deepcopy(state))
            await super().save_gateway_state(state)

    repository = RecordingRepository()
    service = PersistentConnectorGateway(repository)

    async def scenario():
        await service.initialize()
        now = datetime.now(timezone.utc).isoformat()
        async with service._lock:
            for device_id in ("device-heartbeat", "device-unrelated"):
                service.devices[device_id] = {
                    "device_id": device_id,
                    "owner_id": f"owner-{device_id}",
                    "token_hash": "0" * 64,
                    "status": "offline",
                    "created_at": now,
                    "revoked_at": None,
                    "_connection_generation": 0,
                    "event_ack_watermark": 0,
                    "event_pending_sequences": [],
                    "runtimes": [
                        {
                            "runtime_id": "codex",
                            "kind": "codex",
                            "display_name": "Codex",
                        }
                    ],
                }
        await service._persist_current()
        repository.states.clear()

        await service.heartbeat("device-heartbeat", {"active_sessions": 2})

        state = repository.states[-1]
        assert state["_incremental"] is True
        assert [item["device_id"] for item in state["devices"]] == [
            "device-heartbeat"
        ]
        assert state["_replace_runtime_device_ids"] == []
        assert state["pairings"] == []
        assert state["bindings"] == []
        assert state["commands"] == []

    asyncio.run(scenario())


def test_terminal_agent_task_result_survives_gateway_restart():
    class RecordingResultSink:
        def __init__(self):
            self.task_ids: list[str] = []
            self.fail = True

        async def submit(self, result):
            if self.fail:
                raise RuntimeError("simulated Arena Result Sink outage")
            self.task_ids.append(result.task_id)

    repository = MemoryConnectorRepository()
    sink = RecordingResultSink()
    service = PersistentConnectorGateway(
        repository,
        verification_uri="https://arena.example.test/connect",
        agent_task_result_sink=sink,
    )

    async def scenario():
        await service.initialize()
        now = datetime.now(timezone.utc).isoformat()
        task_id = "task-persistent-result-1"
        async with service._lock:
            service.devices["device-result"] = {
                "device_id": "device-result",
                "owner_id": "owner-result",
                "token_hash": "0" * 64,
                "status": "offline",
                "created_at": now,
                "revoked_at": None,
                "_connection_generation": 0,
                "event_ack_watermark": 0,
                "event_pending_sequences": [],
                "runtimes": [],
            }
            service.bindings["binding-result"] = {
                "binding_id": "binding-result",
                "device_id": "device-result",
                "runtime_id": "codex",
                "agent_id": "agent-result",
                "binding_epoch": 7,
                "status": "active",
                "created_at": now,
                "updated_at": now,
            }
            service.commands["command-result"] = {
                "command_id": "command-result",
                "binding_id": "binding-result",
                "device_id": "device-result",
                "status": "delivered",
                "action": CommandAction.TASK_DISPATCH.value,
                "idempotency_key": "persistent-result-once",
                "payload": {"task": {"taskId": task_id}},
                "created_at": now,
                "expires_at": now,
            }
        await service._persist_current()

        payload = {
            "binding_id": "binding-result",
            "binding_epoch": 7,
            "result": {
                "schemaVersion": "arena.agent-result.v1",
                "resultId": "result-persistent-1",
                "taskId": task_id,
                "status": "succeeded",
                "action": {"action": "pass"},
            },
        }
        with pytest.raises(ConnectorError) as exc:
            await service.submit_agent_task_result(
                "device-result",
                payload,
            )
        assert exc.value.status_code == 503
        assert exc.value.detail == "Arena Result Sink unavailable"
        assert repository.gateway_state["agent_task_results"][0]["task_id"] == task_id
        failed_resume = await service.resume_transport(
            "device-result",
            {
                "last_gateway_sequence": 0,
                "event_ack_through": 0,
                "pending_result_ids": ["result-persistent-1"],
            },
        )
        assert failed_resume["accepted_result_ids"] == []

        sink.fail = False
        restarted = PersistentConnectorGateway(
            repository,
            verification_uri="https://arena.example.test/connect",
            agent_task_result_sink=sink,
        )
        await restarted.initialize()
        receipt = await restarted.submit_agent_task_result(
            "device-result",
            payload,
        )
        assert receipt["disposition"] == "replay"
        assert sink.task_ids == [task_id]
        assert restarted.agent_task_results[task_id]["result_id"] == (
            "result-persistent-1"
        )
        final_restart = PersistentConnectorGateway(
            repository,
            verification_uri="https://arena.example.test/connect",
            agent_task_result_sink=sink,
        )
        await final_restart.initialize()
        accepted_resume = await final_restart.resume_transport(
            "device-result",
            {
                "last_gateway_sequence": 0,
                "event_ack_through": 0,
                "pending_result_ids": ["result-persistent-1"],
            },
        )
        assert accepted_resume["accepted_result_ids"] == ["result-persistent-1"]

    asyncio.run(scenario())
