from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from arena_payments.automatic_worker import AutomaticSettlementWorker
from arena_payments.coordinator import X402ExecutionResult, X402SettlementCoordinator
from arena_payments.facilitator import (
    FacilitatorError,
    FacilitatorSettlement,
    ShardedFacilitatorClient,
)
from arena_payments.models import MandateLimits
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


class _PayloadSigner:
    async def create_payment_payload(self, **_: object):
        return {}


class _ClockCapturingCoordinator:
    def __init__(self) -> None:
        self.execution_now: datetime | None = None

    def payment_required(self, _terms):
        return {"accepts": [{"network": "eip155:1439"}]}

    async def execute(self, *, now: datetime, **_: object) -> X402ExecutionResult:
        self.execution_now = now
        return X402ExecutionResult(
            success=True,
            status="submitted",
            transaction="0x" + "55" * 32,
            network="eip155:1439",
        )


class _RoutedCoordinator(_ClockCapturingCoordinator):
    def facilitator_id(self, _payment_required):
        return "shard-3"


class _RoutedRealCoordinator(X402SettlementCoordinator):
    def facilitator_id(self, _payment_required):
        return "shard-3"


class _AmbiguousFacilitator(_Facilitator):
    async def settle(self, **_: object) -> FacilitatorSettlement:
        raise FacilitatorError("facilitator_unreachable", ambiguous=True)


class _Source:
    def __init__(self, mandate) -> None:
        self.mandate = mandate
        self.statuses: list[str] = []
        self.failures: list[str] = []
        self.claims = 0
        self.claimed_facilitator_ids: list[str] = []
        self.recovered: list[tuple[str, str]] = []
        self.payload_digests: list[str | None] = []
        self.facilitator_fence_claims: list[tuple[str, str]] = []
        self.facilitator_fence_releases: list[tuple[str, str]] = []

    async def authorization_targets(self, *, limit: int):
        assert limit == 25
        return ["intent-1"]

    async def settlement_terms(self, settlement_intent_id: str):
        assert settlement_intent_id == "intent-1"
        return _terms()

    async def active_mandate(self, settlement_intent_id: str, now: datetime):
        return self.mandate

    async def claim_attempt(
        self,
        *,
        facilitator_id: str,
        **_: object,
    ):
        self.claims += 1
        self.claimed_facilitator_ids.append(facilitator_id)
        return self.claims == 1

    async def mark_attempt(
        self,
        *,
        status: str,
        payment_payload_digest: str | None = None,
        **_: object,
    ):
        self.statuses.append(status)
        self.payload_digests.append(payment_payload_digest)

    async def claim_facilitator_fence(
        self,
        *,
        facilitator_id: str,
        settlement_intent_id: str,
        **_: object,
    ) -> bool:
        self.facilitator_fence_claims.append(
            (facilitator_id, settlement_intent_id)
        )
        return True

    async def release_facilitator_fence(
        self,
        *,
        facilitator_id: str,
        settlement_intent_id: str,
        **_: object,
    ) -> None:
        self.facilitator_fence_releases.append(
            (facilitator_id, settlement_intent_id)
        )

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


class _UnavailableFenceSource(_Source):
    async def claim_facilitator_fence(self, **kwargs: object) -> bool:
        self.facilitator_fence_claims.append(
            (
                str(kwargs["facilitator_id"]),
                str(kwargs["settlement_intent_id"]),
            )
        )
        return False


class _UnknownPersistenceFailureSource(_Source):
    async def mark_attempt(self, *, status: str, **_: object):
        self.statuses.append(status)
        if status == "unknown":
            raise RuntimeError("attempt persistence unavailable")


class _ConcurrentSource(_Source):
    async def authorization_targets(self, *, limit: int):
        assert limit == 25
        return [f"intent-{index}" for index in range(1, 5)]

    async def settlement_terms(self, settlement_intent_id: str):
        return _terms(settlement_intent_id, amount=20)

    async def claim_attempt(
        self,
        *,
        facilitator_id: str,
        **_: object,
    ):
        self.claimed_facilitator_ids.append(facilitator_id)
        return True


class _ConcurrencyTracker:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0


class _ConcurrentFacilitator:
    def __init__(self, tracker: _ConcurrencyTracker) -> None:
        self.tracker = tracker

    async def verify(self, **_: object) -> bool:
        return True

    async def settle(
        self, *, payment_requirements, **_: object
    ) -> FacilitatorSettlement:
        self.tracker.active += 1
        self.tracker.max_active = max(
            self.tracker.max_active,
            self.tracker.active,
        )
        await asyncio.sleep(0.02)
        self.tracker.active -= 1
        return FacilitatorSettlement(
            success=True,
            transaction=(
                "0x"
                + payment_requirements["extra"]["arena402IntentHash"][
                    -64:
                ]
            ),
            network=payment_requirements["network"],
        )


def test_worker_terminalizes_authorization_when_payment_mandate_is_not_active() -> None:
    payments = InMemoryPaymentRepository()
    source = _Source(None)
    worker = AutomaticSettlementWorker(
        source=source,
        payments=payments,
        signer=_Signer(),
        coordinator=X402SettlementCoordinator(
            payments=payments,
            arena=_Arena(),
            facilitator=_Facilitator(),
        ),
        worker_id="worker-1",
    )

    assert asyncio.run(worker.run_once()) == 1
    assert source.failures == ["payment_mandate_not_active"]
    assert source.claims == 0
    assert source.statuses == []
    assert payments.reservations == {}


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


def test_worker_persists_signed_payment_payload_digest_before_submission() -> None:
    now = datetime.now(timezone.utc)
    mandate = replace(
        _mandate(),
        valid_from=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=1),
    )
    payments = InMemoryPaymentRepository()
    asyncio.run(payments.create_mandate(mandate))
    source = _Source(mandate)
    signer = _Signer()
    coordinator = X402SettlementCoordinator(
        payments=payments,
        arena=_Arena(),
        facilitator=_Facilitator(),
    )
    worker = AutomaticSettlementWorker(
        source=source,
        payments=payments,
        signer=signer,
        coordinator=coordinator,
        worker_id="worker-1",
    )
    payment_required = coordinator.payment_required(_terms())
    payload = asyncio.run(
        signer.create_payment_payload(
            payment_required=payment_required,
            wallet_id=mandate.wallet_id,
            expected_from=_terms().payer,
        )
    )
    expected = "sha256:" + hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    with patch.object(
        signer,
        "create_payment_payload",
        return_value=payload,
    ):
        asyncio.run(worker.run_once())

    assert source.payload_digests == [expected, None, None]


def test_worker_validates_signed_payload_against_post_signing_clock() -> None:
    before_signing = datetime(2026, 7, 26, 1, 2, 3, tzinfo=timezone.utc)
    after_signing = before_signing + timedelta(seconds=2)
    mandate = replace(
        _mandate(),
        valid_from=before_signing - timedelta(hours=1),
        expires_at=before_signing + timedelta(hours=1),
    )
    payments = InMemoryPaymentRepository()
    asyncio.run(payments.create_mandate(mandate))
    source = _Source(mandate)
    coordinator = _ClockCapturingCoordinator()
    worker = AutomaticSettlementWorker(
        source=source,
        payments=payments,
        signer=_PayloadSigner(),
        coordinator=coordinator,  # type: ignore[arg-type]
        worker_id="worker-1",
    )

    with patch("arena_payments.automatic_worker.datetime") as clock:
        clock.now.side_effect = [before_signing, after_signing, after_signing]
        asyncio.run(worker.run_once())

    assert coordinator.execution_now == after_signing


def test_worker_persists_facilitator_route_before_signing() -> None:
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
        signer=_PayloadSigner(),
        coordinator=_RoutedCoordinator(),  # type: ignore[arg-type]
        worker_id="worker-1",
    )

    asyncio.run(worker.run_once())

    assert source.claimed_facilitator_ids == ["shard-3"]
    assert source.statuses == ["signed", "submitting", "submitted"]


def test_worker_holds_durable_facilitator_fence_around_broadcast() -> None:
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
        signer=_Signer(),
        coordinator=_RoutedRealCoordinator(
            payments=payments,
            arena=_Arena(),
            facilitator=_Facilitator(),
        ),
        worker_id="worker-1",
    )

    asyncio.run(worker.run_once())

    expected = [("shard-3", "intent-1")]
    assert source.facilitator_fence_claims == expected
    assert source.facilitator_fence_releases == expected


def test_worker_does_not_broadcast_without_durable_facilitator_fence() -> None:
    now = datetime.now(timezone.utc)
    mandate = replace(
        _mandate(),
        valid_from=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=1),
    )
    payments = InMemoryPaymentRepository()
    asyncio.run(payments.create_mandate(mandate))
    source = _UnavailableFenceSource(mandate)
    worker = AutomaticSettlementWorker(
        source=source,
        payments=payments,
        signer=_Signer(),
        coordinator=_RoutedRealCoordinator(
            payments=payments,
            arena=_Arena(),
            facilitator=_Facilitator(),
        ),
        worker_id="worker-1",
    )

    asyncio.run(worker.run_once())

    expected = [("shard-3", "intent-1")]
    assert source.statuses == ["signed"]
    assert source.facilitator_fence_claims == expected
    assert source.facilitator_fence_releases == []
    assert next(iter(payments.reservations.values())).status == "reserved"


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
    assert source.facilitator_fence_claims == [("configured", "intent-1")]
    assert source.facilitator_fence_releases == []
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


def test_worker_executes_four_facilitator_routes_concurrently() -> None:
    now = datetime.now(timezone.utc)
    mandate = replace(
        _mandate(),
        limits=MandateLimits(
            max_per_payment_atomic=50,
            max_cumulative_atomic=200,
        ),
        valid_from=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=1),
    )
    payments = InMemoryPaymentRepository()
    asyncio.run(payments.create_mandate(mandate))
    source = _ConcurrentSource(mandate)
    tracker = _ConcurrencyTracker()
    coordinator = X402SettlementCoordinator(
        payments=payments,
        arena=_Arena(),
        facilitator=ShardedFacilitatorClient(
            {
                f"shard-{index}": _ConcurrentFacilitator(tracker)
                for index in range(1, 5)
            }
        ),
    )
    worker = AutomaticSettlementWorker(
        source=source,
        payments=payments,
        signer=_Signer(),
        coordinator=coordinator,
        worker_id="worker-1",
        execution_concurrency=4,
    )

    assert asyncio.run(worker.run_once()) == 4
    assert tracker.max_active == 4
    assert len(source.claimed_facilitator_ids) == 4
