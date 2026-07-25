"""Automatic mandate-authorized x402 execution for one frozen settlement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .facilitator import (
    FacilitatorClient,
    FacilitatorError,
)
from .models import SettlementTerms
from .repository import PaymentRepository
from .service import ArenaPaymentService


class ArenaSettlementRecorder(Protocol):
    async def record_mandate_approval(
        self,
        *,
        settlement_intent_id: str,
        approved_intent_hash: str,
        authorization_nonce: str,
    ) -> None: ...

    async def record_automatic_submission(
        self,
        *,
        settlement_intent_id: str,
        tx_hash: str,
        authorization_nonce: str,
        approved_intent_hash: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class X402ExecutionResult:
    success: bool
    status: str
    transaction: str | None
    network: str
    payer: str | None = None
    error_reason: str | None = None

    def to_response(self) -> dict[str, object]:
        value: dict[str, object] = {
            "success": self.success,
            "status": self.status,
            "transaction": self.transaction or "",
            "network": self.network,
        }
        if self.payer is not None:
            value["payer"] = self.payer
        if self.error_reason is not None:
            value["errorReason"] = self.error_reason
        return value


class X402SettlementCoordinator:
    def __init__(
        self,
        *,
        payments: PaymentRepository,
        arena: ArenaSettlementRecorder,
        facilitator: FacilitatorClient,
    ) -> None:
        self._payments = payments
        self._arena = arena
        self._facilitator = facilitator
        self._protocol = ArenaPaymentService(repository=payments)

    def payment_required(self, terms: SettlementTerms) -> dict[str, Any]:
        return self._protocol.payment_required(terms)

    def facilitator_id(
        self,
        payment_requirements: dict[str, Any],
    ) -> str:
        resolver = getattr(self._facilitator, "facilitator_id_for", None)
        if resolver is None:
            return "configured"
        return str(resolver(payment_requirements))

    async def execute(
        self,
        *,
        terms: SettlementTerms,
        mandate_id: str,
        payment_payload: dict[str, Any],
        now: datetime | None = None,
    ) -> X402ExecutionResult:
        clock = now or datetime.now(timezone.utc)
        reservation = await self._payments.reserve_mandate(
            mandate_id=mandate_id,
            terms=terms,
            now=clock,
        )
        requirement = self._protocol.payment_required(terms)["accepts"][0]
        if reservation.status in {"submitted", "consumed"}:
            return X402ExecutionResult(
                success=True,
                status="submitted",
                transaction=reservation.tx_hash,
                network=requirement["network"],
                payer=terms.payer,
            )
        if reservation.status == "released":
            return X402ExecutionResult(
                success=False,
                status="failed",
                transaction=None,
                network=requirement["network"],
                error_reason="payment_reservation_released",
            )
        settle_started = False
        try:
            self._protocol.validate_payment_payload(terms, payment_payload, now=clock)
            verified = await self._facilitator.verify(
                payment_payload=payment_payload,
                payment_requirements=requirement,
            )
            if not verified:
                await self._payments.release_reservation(
                    reservation.reservation_id,
                    reason="facilitator_verification_rejected",
                    now=clock,
                )
                return X402ExecutionResult(
                    success=False,
                    status="failed",
                    transaction=None,
                    network=requirement["network"],
                    error_reason="payment_verification_failed",
                )
            nonce = str(payment_payload["payload"]["authorization"]["nonce"])
            await self._arena.record_mandate_approval(
                settlement_intent_id=terms.settlement_intent_id,
                approved_intent_hash=terms.intent_hash,
                authorization_nonce=nonce,
            )
            # From this point on a timeout or local persistence failure cannot
            # prove that no chain transaction was submitted. Keep the
            # reservation locked for recovery instead of making the budget
            # spendable again.
            settle_started = True
            settled = await self._facilitator.settle(
                payment_payload=payment_payload,
                payment_requirements=requirement,
            )
            if not settled.success or settled.transaction is None:
                await self._payments.release_reservation(
                    reservation.reservation_id,
                    reason="facilitator_settlement_rejected",
                    now=clock,
                )
                return X402ExecutionResult(
                    success=False,
                    status="failed",
                    transaction=None,
                    network=settled.network,
                    payer=settled.payer,
                    error_reason=(settled.error_reason or "payment_settlement_failed"),
                )
            await self._arena.record_automatic_submission(
                settlement_intent_id=terms.settlement_intent_id,
                tx_hash=settled.transaction,
                authorization_nonce=nonce,
                approved_intent_hash=terms.intent_hash,
            )
            await self._payments.submit_reservation(
                reservation.reservation_id,
                tx_hash=settled.transaction,
                now=clock,
            )
            return X402ExecutionResult(
                success=True,
                status="submitted",
                transaction=settled.transaction,
                network=settled.network,
                payer=settled.payer,
            )
        except FacilitatorError as exc:
            if not exc.ambiguous:
                await self._payments.release_reservation(
                    reservation.reservation_id,
                    reason=exc.code,
                    now=clock,
                )
            return X402ExecutionResult(
                success=False,
                status="unknown" if exc.ambiguous else "failed",
                transaction=None,
                network=requirement["network"],
                error_reason=exc.code,
            )
        except Exception:
            if settle_started:
                return X402ExecutionResult(
                    success=False,
                    status="unknown",
                    transaction=None,
                    network=requirement["network"],
                    error_reason="payment_submission_unknown",
                )
            await self._payments.release_reservation(
                reservation.reservation_id,
                reason="payment_pre_submission_failed",
                now=clock,
            )
            raise
