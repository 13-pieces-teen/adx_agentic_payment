from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from arena_payments.automatic_worker import AutomaticSettlementWorker
from arena_payments.coordinator import X402SettlementCoordinator
from arena_payments.facilitator import FacilitatorSettlement
from arena_payments.facilitator import FacilitatorError
from arena_payments.repository import InMemoryPaymentRepository
from tests.test_arena_payments import _mandate, _terms


class _Arena:
    async def record_mandate_approval(self, **_: str) -> None:
        return None

    async def record_automatic_submission(self, **_: str) -> None:
        return None


class _Facilitator:
    async def verify(self, **_: object) -> bool:
        return True

    async def settle(self, **_: object) -> FacilitatorSettlement:
        return FacilitatorSettlement(
            success=True,
            transaction="0x" + "55" * 32,
            network="eip155:1439",
        )


class _Signer:
    async def create_payment_payload(
        self, *, payment_required, wallet_id, expected_from
    ):
        accepted = payment_required["accepts"][0]
        now = datetime.now(timezone.utc)
        return {
            "x402Version": 2,
            "resource": payment_required["resource"],
            "accepted": accepted,
            "payload": {
                "signature": "0x" + "66" * 65,
                "authorization": {
                    "from": expected_from,
                    "to": accepted["payTo"],
                    "value": accepted["amount"],
                    "validAfter": str(int(now.timestamp()) - 1),
                    "validBefore": str(int((now + timedelta(minutes=9)).timestamp())),
                    "nonce": "0x"
                    + accepted["extra"]["arena402IntentHash"].removeprefix("sha256:"),
                },
            },
        }


class _FailingSigner:
    async def create_payment_payload(self, **_: object):
        raise RuntimeError("signer unavailable")


class _AmbiguousFacilitator(_Facilitator):
    async def settle(self, **_: object) -> FacilitatorSettlement:
        raise FacilitatorError("facilitator_unreachable", ambiguous=True)


class _Source:
    def __init__(self, mandate) -> None:
        self.mandate = mandate
        self.statuses: list[str] = []
        self.failures: list[str] = []
        self.claims = 0
        self.recovered: list[tuple[str, str]] = []

    async def authorization_targets(self, *, limit: int):
        assert limit == 25
        return ["intent-1"]

    async def settlement_terms(self, settlement_intent_id: str):
        assert settlement_intent_id == "intent-1"
        return _terms()

    async def active_mandate(self, settlement_intent_id: str, now: datetime):
        return self.mandate

    async def claim_attempt(self, **_: object):
        self.claims += 1
        return self.claims == 1

    async def mark_attempt(self, *, status: str, **_: object):
        self.statuses.append(status)

    async def fail_settlement(self, *, safe_error_code: str, **_: object):
        self.failures.append(safe_error_code)

    async def unknown_submission_targets(self, *, limit: int):
        assert limit == 25
        return []

    async def record_recovered_submission(self, *, settlement_intent_id, tx_hash, **_):
        self.recovered.append((settlement_intent_id, tx_hash))


class _RecoveryReader:
    async def find_transaction_for_authorization(self, terms, **_: object):
        assert terms.settlement_intent_id == "intent-1"
        return "0x" + "77" * 32


class _UnknownSource(_Source):
    async def authorization_targets(self, *, limit: int):
        assert limit == 25
        return []

    async def unknown_submission_targets(self, *, limit: int):
        assert limit == 25
        return ["intent-1"]


class _UnknownPersistenceFailureSource(_Source):
    async def mark_attempt(self, *, status: str, **_: object):
        self.statuses.append(status)
        if status == "unknown":
            raise RuntimeError("attempt persistence unavailable")


def test_worker_runs_wallet_to_x402_to_submission_without_human_gate() -> None:
    now = datetime.now(timezone.utc)
    mandate = replace(
        _mandate(),
        valid_from=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=1),
    )
    payments = InMemoryPaymentRepository()
    asyncio.run(payments.create_mandate(mandate))
    source = _Source(mandate)
    coordinator = X402SettlementCoordinator(
        payments=payments,
        arena=_Arena(),
        facilitator=_Facilitator(),
    )
    worker = AutomaticSettlementWorker(
        source=source,
        payments=payments,
        signer=_Signer(),
        coordinator=coordinator,
        worker_id="worker-1",
    )

    assert asyncio.run(worker.run_once()) == 1
    assert source.statuses == ["signed", "submitting", "submitted"]
    assert payments.mandates["mandate-1"].reserved_atomic == 40
    assert payments.mandates["mandate-1"].consumed_atomic == 0
    reservation = next(iter(payments.reservations.values()))
    assert reservation.status == "submitted"


def test_signer_failure_releases_reserved_mandate_budget() -> None:
    now = datetime.now(timezone.utc)
    mandate = replace(
        _mandate(),
        valid_from=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=1),
    )
    payments = InMemoryPaymentRepository()
    asyncio.run(payments.create_mandate(mandate))
    source = _Source(mandate)
    worker = AutomaticSettlementWorker(
        source=source,
        payments=payments,
        signer=_FailingSigner(),
        coordinator=X402SettlementCoordinator(
            payments=payments,
            arena=_Arena(),
            facilitator=_Facilitator(),
        ),
        worker_id="worker-1",
    )

    try:
        asyncio.run(worker.run_once())
    except RuntimeError as exc:
        assert str(exc) == "signer unavailable"
    else:
        raise AssertionError("expected signer failure")

    assert source.statuses == ["failed"]
    assert source.failures == ["automatic_settlement_failed"]
    assert payments.mandates["mandate-1"].reserved_atomic == 0


def test_unknown_submission_never_releases_budget_on_persistence_failure() -> None:
    now = datetime.now(timezone.utc)
    mandate = replace(
        _mandate(),
        valid_from=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=1),
    )
    payments = InMemoryPaymentRepository()
    asyncio.run(payments.create_mandate(mandate))
    source = _UnknownPersistenceFailureSource(mandate)
    worker = AutomaticSettlementWorker(
        source=source,
        payments=payments,
        signer=_Signer(),
        coordinator=X402SettlementCoordinator(
            payments=payments,
            arena=_Arena(),
            facilitator=_AmbiguousFacilitator(),
        ),
        worker_id="worker-1",
    )

    try:
        asyncio.run(worker.run_once())
    except RuntimeError as exc:
        assert str(exc) == "attempt persistence unavailable"
    else:
        raise AssertionError("expected persistence failure")

    assert source.statuses == ["signed", "submitting", "unknown"]
    assert payments.mandates["mandate-1"].reserved_atomic == 40
    assert next(iter(payments.reservations.values())).status == "reserved"


def test_worker_recovers_unknown_submission_without_signing_or_rebroadcasting() -> None:
    now = datetime.now(timezone.utc)
    mandate = replace(
        _mandate(),
        valid_from=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=1),
    )
    payments = InMemoryPaymentRepository()
    asyncio.run(payments.create_mandate(mandate))
    source = _UnknownSource(mandate)
    worker = AutomaticSettlementWorker(
        source=source,
        payments=payments,
        signer=_FailingSigner(),
        coordinator=X402SettlementCoordinator(
            payments=payments,
            arena=_Arena(),
            facilitator=_Facilitator(),
        ),
        worker_id="worker-1",
        authorization_recovery_reader=_RecoveryReader(),
    )

    assert asyncio.run(worker.run_once()) == 1
    assert source.recovered == [("intent-1", "0x" + "77" * 32)]
    assert source.statuses == []
