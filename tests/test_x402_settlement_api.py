from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena_payments.coordinator import X402SettlementCoordinator
from arena_payments.facilitator import FacilitatorSettlement
from arena_payments.repository import InMemoryPaymentRepository
from arena_payments.x402 import decode_x402_header, encode_x402_header
from arena_payments.x402_api import create_x402_settlement_router
from tests.test_arena_payments import _mandate


@dataclass
class _Intent:
    settlement_intent_id: str = "intent-1"
    intent_hash: str = "sha256:" + "ab" * 32
    game_id: str = "game-1"
    buyer_account: str = "0x" + "11" * 20
    seller_account: str = "0x" + "22" * 20
    chain_id: int = 1439
    token_address: str = "0x" + "33" * 20
    token_symbol: str = "mUSDC"
    token_decimals: int = 6
    token_eip712_name: str = "Mock USD Coin"
    token_eip712_version: str = "1"
    amount_atomic: int = 40


class _Arena:
    def __init__(self) -> None:
        self.intent = _Intent()
        self.approvals = 0
        self.submissions = 0

    async def settlement_intent_for_payment(self, **_: str) -> _Intent:
        return self.intent

    async def record_mandate_approval(self, **_: str) -> None:
        self.approvals += 1

    async def record_automatic_submission(self, **_: str) -> None:
        self.submissions += 1


class _Mandates:
    def __init__(self, mandate):
        self.mandate = mandate

    async def active_mandate_for_settlement(self, **_: object):
        return self.mandate


class _Facilitator:
    async def verify(self, **_: object) -> bool:
        return True

    async def settle(self, **_: object) -> FacilitatorSettlement:
        return FacilitatorSettlement(
            success=True,
            transaction="0x" + "55" * 32,
            network="eip155:1439",
            payer="0x" + "11" * 20,
            facilitator_id="fake",
        )


def _client():
    payments = InMemoryPaymentRepository()
    now = datetime.now(timezone.utc)
    mandate = replace(
        _mandate(),
        valid_from=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=1),
    )
    asyncio.run(payments.create_mandate(mandate))
    arena = _Arena()
    coordinator = X402SettlementCoordinator(
        payments=payments,
        arena=arena,
        facilitator=_Facilitator(),
    )
    app = FastAPI()
    app.include_router(
        create_x402_settlement_router(
            arena=arena,  # type: ignore[arg-type]
            mandates=_Mandates(mandate),
            coordinator=coordinator,
            public_api_url="https://api.arena402.example",
        )
    )
    return TestClient(app), arena, payments


def test_first_request_returns_standard_x402_v2_challenge() -> None:
    client, _, _ = _client()
    response = client.post("/api/v1/x402/settlement-intents/intent-1/execute")
    assert response.status_code == 402
    assert response.headers["cache-control"] == "no-store"
    required = decode_x402_header(response.headers["payment-required"])
    assert required["x402Version"] == 2
    assert required["accepts"][0]["network"] == "eip155:1439"
    assert required["accepts"][0]["amount"] == "40"


def test_legacy_intent_without_frozen_token_domain_is_not_signable() -> None:
    client, arena, _ = _client()
    arena.intent.token_eip712_name = ""
    arena.intent.token_eip712_version = ""

    response = client.post("/api/v1/x402/settlement-intents/intent-1/execute")

    assert response.status_code == 409
    assert response.json()["detail"] == "token_eip712_domain_not_frozen"


def test_retry_with_payment_signature_returns_payment_response() -> None:
    client, arena, payments = _client()
    challenge = client.post("/api/v1/x402/settlement-intents/intent-1/execute")
    required = decode_x402_header(challenge.headers["payment-required"])
    now = datetime.now(timezone.utc)
    payload = {
        "x402Version": 2,
        "resource": required["resource"],
        "accepted": required["accepts"][0],
        "payload": {
            "signature": "0x" + "66" * 65,
            "authorization": {
                "from": "0x" + "11" * 20,
                "to": "0x" + "22" * 20,
                "value": "40",
                "validAfter": str(int(now.timestamp()) - 1),
                "validBefore": str(int((now + timedelta(minutes=9)).timestamp())),
                "nonce": "0x" + "ab" * 32,
            },
        },
    }
    response = client.post(
        "/api/v1/x402/settlement-intents/intent-1/execute",
        headers={"PAYMENT-SIGNATURE": encode_x402_header(payload)},
    )
    assert response.status_code == 200, response.text
    settled = decode_x402_header(response.headers["payment-response"])
    assert settled["success"] is True
    assert settled["transaction"] == "0x" + "55" * 32
    assert arena.approvals == arena.submissions == 1
    assert payments.mandates["mandate-1"].reserved_atomic == 40
    assert payments.mandates["mandate-1"].consumed_atomic == 0
    assert next(iter(payments.reservations.values())).status == "submitted"
