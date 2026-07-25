"""Unattended A2A settlement worker authorized by a PaymentMandate."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from .coordinator import X402SettlementCoordinator
from .models import PaymentMandate, SettlementTerms
from .repository import PaymentRepository
from .signer import WalletSignerClient


class AutomaticSettlementSource(Protocol):
    async def authorization_targets(self, *, limit: int) -> list[str]: ...

    async def settlement_terms(self, settlement_intent_id: str) -> SettlementTerms: ...

    async def active_mandate(
        self, settlement_intent_id: str, now: datetime
    ) -> PaymentMandate | None: ...

    async def claim_attempt(
        self,
        *,
        settlement_intent_id: str,
        reservation_id: str,
        payment_required: dict,
        worker_id: str,
        now: datetime,
    ) -> bool: ...

    async def mark_attempt(
        self,
        *,
        settlement_intent_id: str,
        status: str,
        worker_id: str,
        safe_error_code: str | None = None,
    ) -> None: ...


class AutomaticSettlementWorker:
    def __init__(
        self,
        *,
        source: AutomaticSettlementSource,
        payments: PaymentRepository,
        signer: WalletSignerClient,
        coordinator: X402SettlementCoordinator,
        worker_id: str,
        scan_limit: int = 25,
    ) -> None:
        if not worker_id or not 1 <= scan_limit <= 100:
            raise ValueError("invalid_automatic_settlement_worker")
        self._source = source
        self._payments = payments
        self._signer = signer
        self._coordinator = coordinator
        self._worker_id = worker_id
        self._scan_limit = scan_limit

    async def run_once(self) -> int:
        targets = await self._source.authorization_targets(limit=self._scan_limit)
        for settlement_intent_id in targets:
            await self._execute(settlement_intent_id)
        return len(targets)

    async def _execute(self, settlement_intent_id: str) -> None:
        now = datetime.now(timezone.utc)
        terms = await self._source.settlement_terms(settlement_intent_id)
        mandate = await self._source.active_mandate(settlement_intent_id, now)
        if mandate is None:
            return
        reservation = await self._payments.reserve_mandate(
            mandate_id=mandate.mandate_id,
            terms=terms,
            now=now,
        )
        payment_required = self._coordinator.payment_required(terms)
        claimed = await self._source.claim_attempt(
            settlement_intent_id=settlement_intent_id,
            reservation_id=reservation.reservation_id,
            payment_required=payment_required,
            worker_id=self._worker_id,
            now=now,
        )
        if not claimed:
            return
        try:
            payload = await self._signer.create_payment_payload(
                payment_required=payment_required,
                wallet_id=mandate.wallet_id,
                expected_from=terms.payer,
            )
            await self._source.mark_attempt(
                settlement_intent_id=settlement_intent_id,
                status="signed",
                worker_id=self._worker_id,
            )
            # Persist the ambiguity boundary before the external settle call.
            await self._source.mark_attempt(
                settlement_intent_id=settlement_intent_id,
                status="submitting",
                worker_id=self._worker_id,
            )
            result = await self._coordinator.execute(
                terms=terms,
                mandate_id=mandate.mandate_id,
                payment_payload=payload,
                now=now,
            )
            await self._source.mark_attempt(
                settlement_intent_id=settlement_intent_id,
                status=result.status,
                worker_id=self._worker_id,
                safe_error_code=result.error_reason,
            )
        except Exception:
            await self._payments.release_reservation(
                reservation.reservation_id,
                reason="automatic_settlement_pre_submission_failed",
                now=now,
            )
            await self._source.mark_attempt(
                settlement_intent_id=settlement_intent_id,
                status="failed",
                worker_id=self._worker_id,
                safe_error_code="automatic_settlement_failed",
            )
            raise
