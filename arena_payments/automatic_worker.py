"""Unattended A2A settlement worker authorized by a PaymentMandate."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Protocol

from .coordinator import X402SettlementCoordinator
from .models import PaymentMandate, SettlementTerms
from .repository import PaymentRepository
from .signer import WalletSignerClient


class AuthorizationRecoveryReader(Protocol):
    async def find_transaction_for_authorization(
        self,
        terms: SettlementTerms,
        *,
        lookback_blocks: int = 4_096,
    ) -> str | None: ...


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
        facilitator_id: str,
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
        payment_payload_digest: str | None = None,
    ) -> None: ...

    async def claim_facilitator_fence(
        self,
        *,
        facilitator_id: str,
        settlement_intent_id: str,
        worker_id: str,
        now: datetime,
    ) -> bool: ...

    async def release_facilitator_fence(
        self,
        *,
        facilitator_id: str,
        settlement_intent_id: str,
        worker_id: str,
    ) -> None: ...

    async def fail_settlement(
        self,
        *,
        settlement_intent_id: str,
        safe_error_code: str,
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
        execution_concurrency: int = 1,
        authorization_recovery_reader: AuthorizationRecoveryReader | None = None,
    ) -> None:
        if not worker_id or not 1 <= scan_limit <= 100:
            raise ValueError("invalid_automatic_settlement_worker")
        if not 1 <= execution_concurrency <= 64:
            raise ValueError("invalid_automatic_settlement_concurrency")
        self._source = source
        self._payments = payments
        self._signer = signer
        self._coordinator = coordinator
        self._worker_id = worker_id
        self._scan_limit = scan_limit
        self._execution_concurrency = execution_concurrency
        self._authorization_recovery_reader = authorization_recovery_reader

    async def run_once(self) -> int:
        recovered = await self._recover_ambiguous_submissions()
        targets = await self._source.authorization_targets(limit=self._scan_limit)
        semaphore = asyncio.Semaphore(self._execution_concurrency)

        async def execute_one(settlement_intent_id: str) -> None:
            async with semaphore:
                await self._execute(settlement_intent_id)

        results = await asyncio.gather(
            *(execute_one(settlement_intent_id) for settlement_intent_id in targets),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return recovered + len(targets)

    async def _recover_ambiguous_submissions(self) -> int:
        if self._authorization_recovery_reader is None:
            return 0
        targets_method = getattr(
            self._source, "unknown_submission_targets", None
        )
        record_method = getattr(
            self._source, "record_recovered_submission", None
        )
        if targets_method is None or record_method is None:
            return 0
        targets = await targets_method(limit=self._scan_limit)
        recovered = 0
        for settlement_intent_id in targets:
            terms = await self._source.settlement_terms(settlement_intent_id)
            tx_hash = await self._authorization_recovery_reader.find_transaction_for_authorization(
                terms
            )
            if tx_hash is None:
                continue
            await record_method(
                settlement_intent_id=settlement_intent_id,
                tx_hash=tx_hash,
                now=datetime.now(timezone.utc),
            )
            recovered += 1
        return recovered

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
        resolver = getattr(self._coordinator, "facilitator_id", None)
        facilitator_id = (
            str(resolver(payment_required["accepts"][0]))
            if resolver is not None
            else "configured"
        )
        claimed = await self._source.claim_attempt(
            settlement_intent_id=settlement_intent_id,
            reservation_id=reservation.reservation_id,
            payment_required=payment_required,
            facilitator_id=facilitator_id,
            worker_id=self._worker_id,
            now=now,
        )
        if not claimed:
            return
        submission_may_have_started = False
        fence_claimed = False
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
                payment_payload_digest=_payment_payload_digest(payload),
            )
            fence_claimed = await self._source.claim_facilitator_fence(
                facilitator_id=facilitator_id,
                settlement_intent_id=settlement_intent_id,
                worker_id=self._worker_id,
                now=datetime.now(timezone.utc),
            )
            if not fence_claimed:
                return
            # Persist the ambiguity boundary before the external settle call.
            await self._source.mark_attempt(
                settlement_intent_id=settlement_intent_id,
                status="submitting",
                worker_id=self._worker_id,
            )
            submission_may_have_started = True
            result = await self._coordinator.execute(
                terms=terms,
                mandate_id=mandate.mandate_id,
                payment_payload=payload,
                # The signer creates its EIP-3009 window from its own current
                # clock. Reusing the pre-signing scan timestamp can make an
                # exactly 600-second authorization appear longer than the
                # protocol limit when signing crosses a second boundary.
                now=datetime.now(timezone.utc),
            )
            if result.status == "failed":
                await self._source.fail_settlement(
                    settlement_intent_id=settlement_intent_id,
                    safe_error_code=(
                        result.error_reason or "automatic_settlement_failed"
                    ),
                )
            await self._source.mark_attempt(
                settlement_intent_id=settlement_intent_id,
                status=result.status,
                worker_id=self._worker_id,
                safe_error_code=result.error_reason,
            )
            if result.status in {"submitted", "failed"}:
                await self._source.release_facilitator_fence(
                    facilitator_id=facilitator_id,
                    settlement_intent_id=settlement_intent_id,
                    worker_id=self._worker_id,
                )
        except Exception:
            if not submission_may_have_started:
                if fence_claimed:
                    await self._source.release_facilitator_fence(
                        facilitator_id=facilitator_id,
                        settlement_intent_id=settlement_intent_id,
                        worker_id=self._worker_id,
                    )
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
                await self._source.fail_settlement(
                    settlement_intent_id=settlement_intent_id,
                    safe_error_code="automatic_settlement_failed",
                )
            raise


def _payment_payload_digest(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()
