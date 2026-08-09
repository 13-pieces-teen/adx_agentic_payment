"""Unattended A2A settlement worker authorized by a PaymentMandate."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .coordinator import X402SettlementCoordinator
from .models import PaymentMandate, SettlementTerms
from .repository import PaymentRepository
from .signer import WalletSignerClient


_LOGGER = logging.getLogger(__name__)


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

    async def defer_attempt(
        self,
        *,
        settlement_intent_id: str,
        worker_id: str,
        retry_at: datetime,
    ) -> None: ...

    async def fail_settlement(
        self,
        *,
        settlement_intent_id: str,
        safe_error_code: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _PreparedSettlement:
    settlement_intent_id: str
    terms: SettlementTerms
    payment_required: dict
    facilitator_id: str


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
        prepared_results = await asyncio.gather(
            *(self._prepare(settlement_intent_id) for settlement_intent_id in targets),
            return_exceptions=True,
        )
        prepared: list[_PreparedSettlement] = []
        errors: list[BaseException] = []
        for result in prepared_results:
            if isinstance(result, BaseException):
                errors.append(result)
            else:
                prepared.append(result)

        queues: dict[str, list[_PreparedSettlement]] = {}
        for target in prepared:
            queues.setdefault(target.facilitator_id, []).append(target)
        if prepared:
            _LOGGER.info(
                "settlement_facilitator_queue_scan targets=%d shards=%d depths=%s",
                len(prepared),
                len(queues),
                json.dumps(
                    {
                        facilitator_id: len(queue)
                        for facilitator_id, queue in sorted(queues.items())
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        semaphore = asyncio.Semaphore(self._execution_concurrency)

        async def execute_queue(queue: list[_PreparedSettlement]) -> None:
            for target in queue:
                async with semaphore:
                    await self._execute(target)

        results = await asyncio.gather(
            *(execute_queue(queue) for queue in queues.values()),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                errors.append(result)
        if errors:
            raise errors[0]
        return recovered + len(targets)

    async def _prepare(self, settlement_intent_id: str) -> _PreparedSettlement:
        terms = await self._source.settlement_terms(settlement_intent_id)
        payment_required = self._coordinator.payment_required(terms)
        resolver = getattr(self._coordinator, "facilitator_id", None)
        facilitator_id = (
            str(resolver(payment_required["accepts"][0]))
            if resolver is not None
            else "configured"
        )
        return _PreparedSettlement(
            settlement_intent_id=settlement_intent_id,
            terms=terms,
            payment_required=payment_required,
            facilitator_id=facilitator_id,
        )

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

    async def _execute(self, prepared: _PreparedSettlement) -> None:
        settlement_intent_id = prepared.settlement_intent_id
        now = datetime.now(timezone.utc)
        terms = prepared.terms
        mandate = await self._source.active_mandate(settlement_intent_id, now)
        if mandate is None:
            await self._source.fail_settlement(
                settlement_intent_id=settlement_intent_id,
                safe_error_code="payment_mandate_not_active",
            )
            return
        reservation = await self._payments.reserve_mandate(
            mandate_id=mandate.mandate_id,
            terms=terms,
            now=now,
        )
        payment_required = prepared.payment_required
        facilitator_id = prepared.facilitator_id
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
            fence_now = datetime.now(timezone.utc)
            fence_claimed = await self._source.claim_facilitator_fence(
                facilitator_id=facilitator_id,
                settlement_intent_id=settlement_intent_id,
                worker_id=self._worker_id,
                now=fence_now,
            )
            if not fence_claimed:
                retry_at = (
                    fence_now
                    + _facilitator_retry_delay(settlement_intent_id)
                )
                await self._source.defer_attempt(
                    settlement_intent_id=settlement_intent_id,
                    worker_id=self._worker_id,
                    retry_at=retry_at,
                )
                _LOGGER.info(
                    "settlement_facilitator_deferred facilitator_id=%s "
                    "settlement_intent_id=%s retry_at=%s",
                    facilitator_id,
                    settlement_intent_id,
                    retry_at.isoformat(),
                )
                return
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


def _facilitator_retry_delay(settlement_intent_id: str) -> timedelta:
    digest = hashlib.sha256(settlement_intent_id.encode("utf-8")).digest()
    jitter_ms = int.from_bytes(digest[:2], "big") % 1_000
    return timedelta(milliseconds=500 + jitter_ms)
