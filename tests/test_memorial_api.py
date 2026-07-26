from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena_memorial import (
    InMemoryMemorialRepository,
    MemorialAward,
    MemorialStats,
    create_memorial_router,
)
from connector_gateway.config import ConnectorGatewayConfig
from connector_gateway.production import build_production_connector
from connector_gateway.repository import MemoryConnectorRepository


def _app(
    *,
    award: MemorialAward | None = None,
    status: str = "active",
):
    users = MemoryConnectorRepository()
    config = ConnectorGatewayConfig(
        database_url="postgresql://unused-in-memory",
        session_secret="session-secret-that-is-more-than-32-characters",
        public_app_url="https://arena.example.test",
        bootstrap_invite_hash=hashlib.sha256(
            b"unused-invite-that-is-long-enough"
        ).hexdigest(),
    )
    connector = build_production_connector(config, users)
    repository = InMemoryMemorialRepository(
        awards=(award,) if award is not None else (),
        stats=MemorialStats(
            campaign_id="arena402-genesis",
            name="Arena 402 Memorial",
            symbol="arena402",
            chain_id=1439,
            contract_address="0x" + "ab" * 20,
            campaign_status=status,
            max_supply=402,
            reserved_count=1 if award is not None else 0,
            submitted_count=0,
            minted_count=1 if award and award.mint_status == "minted" else 0,
        ),
    )
    app = FastAPI()
    app.include_router(connector.router)
    app.include_router(
        create_memorial_router(auth=connector.auth, repository=repository)
    )
    return (
        TestClient(app, base_url="https://arena.example.test"),
        connector.auth,
        users,
    )


def _login_github(client, auth, users, user_id: str = "founder-user") -> None:
    record = users.users.setdefault(
        user_id,
        {
            "user_id": user_id,
            "username": "octocat",
            "password_hash": None,
            "temporary": False,
            "identity_provider": "github",
            "provider_subject": "123456",
            "created_at": datetime(2026, 7, 26, tzinfo=timezone.utc),
            "disabled_at": None,
        },
    )
    users.users_by_name["octocat"] = user_id
    users.oauth_users[("github", "123456")] = user_id
    issued = asyncio.run(auth._issue_session(record))
    auth.set_session_cookies(
        type(
            "CookieSink",
            (),
            {
                "set_cookie": lambda self, key, value, **kwargs: (
                    client.cookies.set(key, value)
                )
            },
        )(),
        issued,
    )


def test_founder_can_read_public_award_without_any_secret_material() -> None:
    award = MemorialAward(
        campaign_id="arena402-genesis",
        user_id="founder-user",
        registration_rank=1,
        token_id=0,
        wallet_id="memorial-wallet-0000",
        wallet_address="0x" + "11" * 20,
        registered_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
        eligibility_status="reserved",
        mint_status="minted",
        credential_status="unclaimed",
        contract_address="0x" + "ab" * 20,
        mint_tx_hash="0x" + "cd" * 32,
        mint_block_number=123,
        assigned_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
        submitted_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
        minted_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
    )
    client, auth, users = _app(award=award)
    _login_github(client, auth, users)

    response = client.get("/api/v1/me/memorial")

    assert response.status_code == 200
    assert response.json()["eligible"] is True
    assert response.json()["registrationRank"] == 1
    assert response.json()["tokenId"] == 0
    assert response.json()["status"] == "minted"
    serialized = str(response.json()).lower()
    assert "mnemonic" not in serialized
    assert "private_key" not in serialized
    assert "secret" not in serialized


def test_memorial_stats_are_public_and_do_not_disclose_users() -> None:
    client, _, _ = _app()

    response = client.get("/api/v1/memorial/stats")

    assert response.status_code == 200
    assert response.json() == {
        "campaign": "arena402-genesis",
        "name": "Arena 402 Memorial",
        "symbol": "arena402",
        "chainId": 1439,
        "contractAddress": "0x" + "ab" * 20,
        "status": "active",
        "editionSize": 402,
        "reserved": 0,
        "submitted": 0,
        "minted": 0,
        "remaining": 402,
    }


def test_password_identity_is_not_counted_as_a_founding_registration() -> None:
    client, _, _ = _app()
    client.post(
        "/api/auth/invite",
        json={
            "invite_code": "unused-invite-that-is-long-enough",
            "username": "password-user",
            "password": "correct horse battery staple",
        },
    )

    response = client.get("/api/v1/me/memorial")

    assert response.status_code == 200
    assert response.json()["eligible"] is False
    assert response.json()["reason"] == "github_identity_required"
