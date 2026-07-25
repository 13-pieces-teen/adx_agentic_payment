from __future__ import annotations

import asyncio
from datetime import timedelta

from arena_payments.coordinator import X402SettlementCoordinator
from arena_payments.facilitator import (
    FacilitatorError,
    FacilitatorSettlement,
)
from arena_payments.repository import InMemoryPaymentRepository
from arena_payments.service import ArenaPaymentService
from tests.test_arena_payments import NOW, _mandate, _terms


class _Arena:
    def __init__(self, *, submission_error: bool = False) -> None:
        self.approvals: list[dict[str, str]] = []
        self.submissions: list[dict[str, str]] = []
        self.submission_error = submission_error

    async def record_mandate_approval(self, **values: str) -> None:
        self.approvals.append(values)

    async def record_automatic_submission(self, **values: str) -> None:
        if self.submission_error:
            raise RuntimeError("database unavailable")
        self.submissions.append(values)


class _Facilitator:
    def __init__(
        self,
        *,
        verified: bool = True,
        settled: FacilitatorSettlement | None = None,
        error: FacilitatorError | None = None,
    ) -> None:
        self.verified = verified
        self.settled = settled or FacilitatorSettlement(
            success=True,
            transaction="0x" + "55" * 32,
            network="eip155:1439",
            payer=_terms().payer,
            facilitator_id="fake",
        )
        self.error = error

    async def verify(self, **_: object) -> bool:
        return self.verified

    async def settle(self, **_: object) -> FacilitatorSettlement:
        if self.error is not None:
            raise self.error
        return self.settled


def _payload() -> dict:
    terms = _terms()
    challenge = ArenaPaymentService(
        repository=InMemoryPaymentRepository()
    ).payment_required(terms)
    return {
        "x402Version": 2,
        "resource": challenge["resource"],
        "accepted": challenge["accepts"][0],
        "payload": {
            "signature": "0x" + "66" * 65,
            "authorization": {
                "from": terms.payer,
                "to": terms.payee,
                "value": str(terms.amount_atomic),
                "validAfter": str(int(NOW.timestamp()) - 1),
                "validBefore": str(int((NOW + timedelta(minutes=9)).timestamp())),
                "nonce": "0x" + terms.intent_hash.removeprefix("sha256:"),
            },
        },
    }


def _coordinator(facilitator: _Facilitator, *, arena: _Arena | None = None):
    payments = InMemoryPaymentRepository()
    asyncio.run(payments.create_mandate(_mandate()))
    arena = arena or _Arena()
    return (
        X402SettlementCoordinator(
            payments=payments,
            arena=arena,
            facilitator=facilitator,
        ),
        payments,
        arena,
    )


def test_success_records_submission_but_waits_for_chain_confirmation() -> None:
    coordinator, payments, arena = _coordinator(_Facilitator())
    result = asyncio.run(
        coordinator.execute(
            terms=_terms(),
            mandate_id="mandate-1",
            payment_payload=_payload(),
            now=NOW,
        )
    )
    assert result.success is True
    assert result.status == "submitted"
    assert len(arena.approvals) == len(arena.submissions) == 1
    mandate = payments.mandates["mandate-1"]
    assert mandate.reserved_atomic == 40
    assert mandate.consumed_atomic == 0
    reservation = next(iter(payments.reservations.values()))
    assert reservation.status == "submitted"

    repeated = asyncio.run(
        coordinator.execute(
            terms=_terms(),
            mandate_id="mandate-1",
            payment_payload=_payload(),
            now=NOW,
        )
    )
    assert repeated.success is True
    assert payments.mandates["mandate-1"].reserved_atomic == 40
    assert len(arena.submissions) == 1


def test_definite_facilitator_rejection_releases_budget() -> None:
    coordinator, payments, arena = _coordinator(_Facilitator(verified=False))
    result = asyncio.run(
        coordinator.execute(
            terms=_terms(),
            mandate_id="mandate-1",
            payment_payload=_payload(),
            now=NOW,
        )
    )
    assert result.status == "failed"
    assert arena.approvals == arena.submissions == []
    assert payments.mandates["mandate-1"].reserved_atomic == 0


def test_ambiguous_settle_keeps_reservation_for_recovery() -> None:
    coordinator, payments, arena = _coordinator(
        _Facilitator(
            error=FacilitatorError(
                "facilitator_unreachable",
                ambiguous=True,
            )
        )
    )
    result = asyncio.run(
        coordinator.execute(
            terms=_terms(),
            mandate_id="mandate-1",
            payment_payload=_payload(),
            now=NOW,
        )
    )
    assert result.status == "unknown"
    assert len(arena.approvals) == 1
    assert arena.submissions == []
    assert payments.mandates["mandate-1"].reserved_atomic == 40


def test_post_settle_persistence_failure_keeps_reservation_for_recovery() -> None:
    coordinator, payments, arena = _coordinator(
        _Facilitator(),
        arena=_Arena(submission_error=True),
    )

    result = asyncio.run(
        coordinator.execute(
            terms=_terms(),
            mandate_id="mandate-1",
            payment_payload=_payload(),
            now=NOW,
        )
    )

    assert result.status == "unknown"
    assert result.error_reason == "payment_submission_unknown"
    assert len(arena.approvals) == 1
    assert arena.submissions == []
    assert payments.mandates["mandate-1"].reserved_atomic == 40
