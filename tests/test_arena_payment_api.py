from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena_wallets.service import InjectiveWalletService
from arena_payments.api import create_payment_account_router
from arena_payments.models import WalletInventoryItem
from arena_payments.repository import InMemoryPaymentRepository
from connector_gateway.config import ConnectorGatewayConfig
from connector_gateway.production import build_production_connector
from connector_gateway.repository import MemoryConnectorRepository


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _app():
    connector_repository = MemoryConnectorRepository()
    config = ConnectorGatewayConfig(
        database_url="postgresql://unused-in-memory",
        session_secret="session-secret-that-is-more-than-32-characters",
        public_app_url="https://arena.example.test",
        bootstrap_invite_hash=_hash("invite-one-that-is-long-enough"),
    )
    connector = build_production_connector(config, connector_repository)
    payment_repository = InMemoryPaymentRepository(
        [
            WalletInventoryItem(
                wallet_id="wallet-1",
                chain_id=1439,
                address="0x" + "11" * 20,
                secret_ref="agent-wallets.csv#1",
            )
        ]
    )
    wallet_service = InjectiveWalletService(
        "http://127.0.0.1:8545",
        rpc_call=lambda method, params: {
            "eth_chainId": "0x59f",
            "eth_getBalance": "0x0",
        }[method],
    )
    app = FastAPI()
    app.include_router(connector.router)
    app.include_router(
        create_payment_account_router(
            auth=connector.auth,
            repository=payment_repository,
            wallet_service=wallet_service,
        )
    )
    return (
        TestClient(app, base_url="https://arena.example.test"),
        connector.auth,
        connector_repository,
        payment_repository,
    )


def test_wallet_endpoint_binds_once_and_never_returns_secret_reference() -> None:
    client, _, _, _ = _app()
    client.post(
        "/api/auth/invite",
        json={
            "invite_code": "invite-one-that-is-long-enough",
            "username": "password-user",
            "password": "correct horse battery staple",
        },
    )
    denied = client.get("/api/v1/me/wallet")
    assert denied.status_code == 403


def test_github_wallet_and_mandate_api() -> None:
    client, connector_auth, users, payments = _app()

    user = users.users.setdefault(
        "user-github",
        {
            "user_id": "user-github",
            "username": "octocat",
            "password_hash": None,
            "temporary": False,
            "identity_provider": "github",
            "provider_subject": "123456",
            "created_at": datetime.now(timezone.utc),
            "disabled_at": None,
        },
    )
    users.users_by_name["octocat"] = "user-github"
    users.oauth_users[("github", "123456")] = "user-github"
    issued = __import__("asyncio").run(connector_auth._issue_session(user))
    connector_auth.set_session_cookies(
        type(
            "CookieSink",
            (),
            {
                "set_cookie": lambda self, key, value, **kwargs: client.cookies.set(
                    key, value
                )
            },
        )(),
        issued,
    )

    first = client.get("/api/v1/me/wallet")
    second = client.get("/api/v1/me/wallet")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["wallet"]["walletId"] == "wallet-1"
    assert "secret" not in str(first.json()).lower()

    overview = client.get("/api/v1/me/wallet/overview")
    assert overview.status_code == 200, overview.text
    assert overview.json()["walletId"] == "wallet-1"
    assert overview.json()["address"] == "0x" + "11" * 20
    assert overview.json()["chainId"] == 1439
    assert overview.json()["network"] == "injective-testnet"
    assert overview.json()["native"] == {"symbol": "INJ", "balance": "0"}
    assert overview.json()["tokens"] == []
    assert "secret" not in str(overview.json()).lower()

    now = datetime.now(timezone.utc)
    created = client.post(
        "/api/v1/me/payment-mandates",
        headers={"x-csrf-token": issued.csrf_token},
        json={
            "mandateId": "mandate-game-1",
            "gameId": "game-1",
            "chainId": 1439,
            "tokenAddress": "0x" + "33" * 20,
            "maxPerPaymentAtomic": 50,
            "maxCumulativeAtomic": 100,
            "allowedPayees": ["0x" + "22" * 20],
            "validFrom": (now - timedelta(seconds=1)).isoformat(),
            "expiresAt": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["mandate"]["consumedAtomic"] == "0"
    active = client.get("/api/v1/me/payment-mandates/game-1")
    assert active.status_code == 200
    assert active.json()["mandate"]["mandateId"] == "mandate-game-1"

    revoked = client.post(
        "/api/v1/me/payment-mandates/mandate-game-1/revoke",
        headers={"x-csrf-token": issued.csrf_token},
    )
    assert revoked.status_code == 200
    assert revoked.json()["mandate"]["revokedAt"] is not None
    assert payments.mandates["mandate-game-1"].revoked_at is not None


def test_payment_mandate_api_supports_pre_join_dynamic_payee_scope() -> None:
    client, connector_auth, users, _ = _app()
    user = users.users.setdefault(
        "user-github-dynamic",
        {
            "user_id": "user-github-dynamic",
            "username": "dynamic-octocat",
            "password_hash": None,
            "temporary": False,
            "identity_provider": "github",
            "provider_subject": "654321",
            "created_at": datetime.now(timezone.utc),
            "disabled_at": None,
        },
    )
    users.users_by_name["dynamic-octocat"] = "user-github-dynamic"
    users.oauth_users[("github", "654321")] = "user-github-dynamic"
    issued = __import__("asyncio").run(connector_auth._issue_session(user))
    connector_auth.set_session_cookies(
        type(
            "CookieSink",
            ( ),
            {
                "set_cookie": lambda self, key, value, **kwargs: client.cookies.set(
                    key, value
                )
            },
        )(),
        issued,
    )
    assert client.get("/api/v1/me/wallet").status_code == 200
    now = datetime.now(timezone.utc)
    created = client.post(
        "/api/v1/me/payment-mandates",
        headers={"x-csrf-token": issued.csrf_token},
        json={
            "mandateId": "mandate-current-game",
            "gameId": "current-game",
            "chainId": 1439,
            "tokenAddress": "0x" + "33" * 20,
            "maxPerPaymentAtomic": 50,
            "maxCumulativeAtomic": 100,
            "allowedPayeeRule": "SAME_GAME_SETTLEMENT_ACCOUNT",
            "joinAuthorizationId": "ja:current-game",
            "validFrom": (now - timedelta(seconds=1)).isoformat(),
            "expiresAt": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert created.status_code == 201, created.text
    mandate = created.json()["mandate"]
    assert mandate["allowedPayees"] == []
    assert mandate["allowedPayeeRule"] == "SAME_GAME_SETTLEMENT_ACCOUNT"
    assert mandate["joinAuthorizationId"] == "ja:current-game"
