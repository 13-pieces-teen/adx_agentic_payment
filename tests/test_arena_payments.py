from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from arena_payments.models import (
    MandateLimits,
    PaymentMandate,
    SettlementTerms,
    WalletInventoryItem,
)
from arena_payments.repository import (
    InMemoryPaymentRepository,
    MandateRejected,
    WalletUnavailable,
)
from arena_payments.service import ArenaPaymentService
from arena_payments.x402 import (
    X402ProtocolError,
    decode_x402_header,
    encode_x402_header,
)


NOW = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)
BUYER = "0x" + "11" * 20
SELLER = "0x" + "22" * 20
TOKEN = "0x" + "33" * 20


def _wallet(index: int) -> WalletInventoryItem:
    return WalletInventoryItem(
        wallet_id=f"agent-wallet-{index:04d}",
        chain_id=1439,
        address="0x" + f"{index:040x}",
        secret_ref=f"agent-wallets.csv#{index}",
    )


def _mandate(**changes: object) -> PaymentMandate:
    value = PaymentMandate(
        mandate_id="mandate-1",
        user_id="user-1",
        wallet_id="agent-wallet-0001",
        game_id="game-1",
        chain_id=1439,
        token_address=TOKEN,
        limits=MandateLimits(
            max_per_payment_atomic=50,
            max_cumulative_atomic=100,
        ),
        allowed_payees=(SELLER,),
        valid_from=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    return replace(value, **changes)


def _terms(intent_id: str = "intent-1", amount: int = 40) -> SettlementTerms:
    return SettlementTerms(
        settlement_intent_id=intent_id,
        intent_hash="sha256:" + "ab" * 32,
        game_id="game-1",
        payer=BUYER,
        payee=SELLER,
        chain_id=1439,
        token_address=TOKEN,
        token_symbol="mUSDC",
        token_decimals=6,
        token_eip712_name="Mock USD Coin",
        token_eip712_version="1",
        amount_atomic=amount,
        resource_url=(
            "https://api.arena402.example/api/v1/x402/"
            f"settlement-intents/{intent_id}/execute"
        ),
    )


def test_github_user_keeps_the_same_wallet_across_logins_and_games() -> None:
    repository = InMemoryPaymentRepository([_wallet(1), _wallet(2)])

    first = asyncio.run(
        repository.get_or_bind_wallet(
            user_id="user-1",
            identity_provider="github",
            provider_subject="123456",
            now=NOW,
        )
    )
    second = asyncio.run(
        repository.get_or_bind_wallet(
            user_id="user-1",
            identity_provider="github",
            provider_subject="123456",
            now=NOW + timedelta(days=1),
        )
    )

    assert first == second
    assert first.wallet_id == "agent-wallet-0001"
    assert first.address == _wallet(1).address
    assert len(repository.wallet_bindings) == 1


def test_wallet_binding_is_github_only_and_never_reassigns_an_address() -> None:
    repository = InMemoryPaymentRepository([_wallet(1)])

    with pytest.raises(WalletUnavailable, match="github_identity_required"):
        asyncio.run(
            repository.get_or_bind_wallet(
                user_id="password-user",
                identity_provider="password",
                provider_subject=None,
                now=NOW,
            )
        )

    asyncio.run(
        repository.get_or_bind_wallet(
            user_id="user-1",
            identity_provider="github",
            provider_subject="123",
            now=NOW,
        )
    )
    with pytest.raises(WalletUnavailable, match="wallet_pool_exhausted"):
        asyncio.run(
            repository.get_or_bind_wallet(
                user_id="user-2",
                identity_provider="github",
                provider_subject="456",
                now=NOW,
            )
        )


def test_concurrent_login_creates_exactly_one_wallet_binding() -> None:
    async def bind_twice():
        repository = InMemoryPaymentRepository([_wallet(1), _wallet(2)])
        values = await asyncio.gather(
            *[
                repository.get_or_bind_wallet(
                    user_id="user-1",
                    identity_provider="github",
                    provider_subject="123",
                    now=NOW,
                )
                for _ in range(20)
            ]
        )
        return repository, values

    repository, values = asyncio.run(bind_twice())
    assert {value.wallet_id for value in values} == {"agent-wallet-0001"}
    assert len(repository.wallet_bindings) == 1


def test_mandate_reservations_are_atomic_bounded_and_idempotent() -> None:
    async def reserve_concurrently():
        repository = InMemoryPaymentRepository()
        await repository.create_mandate(_mandate())
        results = await asyncio.gather(
            repository.reserve_mandate(
                mandate_id="mandate-1",
                terms=_terms("intent-1", 50),
                now=NOW,
            ),
            repository.reserve_mandate(
                mandate_id="mandate-1",
                terms=_terms("intent-2", 50),
                now=NOW,
            ),
        )
        return repository, results

    repository, results = asyncio.run(reserve_concurrently())
    assert {item.status for item in results} == {"reserved"}
    assert repository.mandates["mandate-1"].reserved_atomic == 100

    duplicate = asyncio.run(
        repository.reserve_mandate(
            mandate_id="mandate-1",
            terms=_terms("intent-1", 50),
            now=NOW,
        )
    )
    assert duplicate.reservation_id == results[0].reservation_id
    assert repository.mandates["mandate-1"].reserved_atomic == 100

    with pytest.raises(MandateRejected, match="mandate_cumulative_limit"):
        asyncio.run(
            repository.reserve_mandate(
                mandate_id="mandate-1",
                terms=_terms("intent-3", 1),
                now=NOW,
            )
        )


@pytest.mark.parametrize(
    ("mandate", "terms", "now", "error"),
    [
        (_mandate(), _terms(amount=51), NOW, "mandate_per_payment_limit"),
        (
            _mandate(allowed_payees=("0x" + "44" * 20,)),
            _terms(),
            NOW,
            "mandate_payee_not_allowed",
        ),
        (
            _mandate(expires_at=NOW),
            _terms(),
            NOW,
            "mandate_expired",
        ),
        (
            _mandate(revoked_at=NOW - timedelta(seconds=1)),
            _terms(),
            NOW,
            "mandate_revoked",
        ),
    ],
)
def test_mandate_rejects_out_of_scope_payment(
    mandate: PaymentMandate,
    terms: SettlementTerms,
    now: datetime,
    error: str,
) -> None:
    repository = InMemoryPaymentRepository()
    asyncio.run(repository.create_mandate(mandate))
    with pytest.raises(MandateRejected, match=error):
        asyncio.run(
            repository.reserve_mandate(
                mandate_id=mandate.mandate_id,
                terms=terms,
                now=now,
            )
        )


def test_release_and_consume_are_idempotent_and_preserve_accounting() -> None:
    repository = InMemoryPaymentRepository()
    asyncio.run(repository.create_mandate(_mandate()))
    reservation = asyncio.run(
        repository.reserve_mandate(
            mandate_id="mandate-1",
            terms=_terms(amount=40),
            now=NOW,
        )
    )

    released = asyncio.run(
        repository.release_reservation(
            reservation.reservation_id,
            reason="facilitator_unavailable",
            now=NOW,
        )
    )
    released_again = asyncio.run(
        repository.release_reservation(
            reservation.reservation_id,
            reason="facilitator_unavailable",
            now=NOW,
        )
    )
    assert released == released_again
    assert repository.mandates["mandate-1"].reserved_atomic == 0
    assert repository.mandates["mandate-1"].consumed_atomic == 0

    second = asyncio.run(
        repository.reserve_mandate(
            mandate_id="mandate-1",
            terms=_terms("intent-2", 40),
            now=NOW,
        )
    )
    consumed = asyncio.run(
        repository.consume_reservation(
            second.reservation_id,
            tx_hash="0x" + "55" * 32,
            now=NOW,
        )
    )
    consumed_again = asyncio.run(
        repository.consume_reservation(
            second.reservation_id,
            tx_hash="0x" + "55" * 32,
            now=NOW,
        )
    )
    assert consumed == consumed_again
    assert repository.mandates["mandate-1"].reserved_atomic == 0
    assert repository.mandates["mandate-1"].consumed_atomic == 40


def test_x402_v2_challenge_is_exact_and_bound_to_frozen_intent() -> None:
    repository = InMemoryPaymentRepository()
    service = ArenaPaymentService(repository=repository)

    challenge = service.payment_required(_terms())
    encoded = encode_x402_header(challenge)
    decoded = decode_x402_header(encoded)

    assert decoded["x402Version"] == 2
    assert decoded["resource"]["url"].endswith("/intent-1/execute")
    assert decoded["accepts"] == [
        {
            "scheme": "exact",
            "network": "eip155:1439",
            "asset": TOKEN,
            "amount": "40",
            "payTo": SELLER,
            "maxTimeoutSeconds": 600,
            "extra": {
                "name": "Mock USD Coin",
                "version": "1",
                "arena402IntentHash": "sha256:" + "ab" * 32,
                "arena402SettlementIntentId": "intent-1",
            },
        }
    ]
    assert base64.b64decode(encoded).startswith(b"{")


def test_x402_payload_must_copy_the_exact_accepted_requirement() -> None:
    repository = InMemoryPaymentRepository()
    service = ArenaPaymentService(repository=repository)
    challenge = service.payment_required(_terms())
    payload = {
        "x402Version": 2,
        "resource": challenge["resource"],
        "accepted": {
            **challenge["accepts"][0],
            "amount": "39",
        },
        "payload": {"signature": "0x" + "66" * 65, "authorization": {}},
    }

    with pytest.raises(X402ProtocolError, match="payment_requirement_mismatch"):
        service.validate_payment_payload(_terms(), payload)


def test_x402_authorization_is_bound_to_payer_payee_amount_nonce_and_time() -> None:
    service = ArenaPaymentService(repository=InMemoryPaymentRepository())
    challenge = service.payment_required(_terms())
    payload = {
        "x402Version": 2,
        "resource": challenge["resource"],
        "accepted": challenge["accepts"][0],
        "payload": {
            "signature": "0x" + "66" * 65,
            "authorization": {
                "from": BUYER,
                "to": SELLER,
                "value": "40",
                "validAfter": str(int(NOW.timestamp()) - 1),
                "validBefore": str(int(NOW.timestamp()) + 599),
                "nonce": "0x" + "ab" * 32,
            },
        },
    }
    assert (
        service.validate_payment_payload(_terms(), payload, now=NOW)
        == payload["payload"]
    )

    payload["payload"]["authorization"]["to"] = BUYER
    with pytest.raises(X402ProtocolError, match="authorization_intent_mismatch"):
        service.validate_payment_payload(_terms(), payload, now=NOW)


def test_x402_headers_reject_non_object_and_invalid_base64() -> None:
    with pytest.raises(X402ProtocolError, match="invalid_x402_header"):
        decode_x402_header("not base64")
    encoded_array = base64.b64encode(json.dumps([]).encode()).decode()
    with pytest.raises(X402ProtocolError, match="invalid_x402_header"):
        decode_x402_header(encoded_array)
