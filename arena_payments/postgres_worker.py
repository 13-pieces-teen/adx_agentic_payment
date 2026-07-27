"""PostgreSQL lease adapter for the automatic settlement worker."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from arena_game.postgres import PostgresPawnhouseRepository

from .models import PaymentMandate, SettlementTerms
from .postgres import PostgresPaymentRepository


class PostgresAutomaticSettlementSource:
    def __init__(
        self,
        *,
        payments: PostgresPaymentRepository,
        arena: PostgresPawnhouseRepository,
        public_api_url: str,
        lease_seconds: int = 60,
        settlement_intent_id: str | None = None,
    ) -> None:
        if not 15 <= lease_seconds <= 600:
            raise ValueError("automatic_payment_lease_seconds_out_of_range")
        if settlement_intent_id is not None and (
            not settlement_intent_id
            or len(settlement_intent_id) > 512
        ):
            raise ValueError("invalid_automatic_settlement_intent_id")
        self._payments = payments
        self._arena = arena
        self._public_api_url = public_api_url.rstrip("/")
        self._lease_seconds = lease_seconds
        self._settlement_intent_id = settlement_intent_id

    async def authorization_targets(self, *, limit: int) -> list[str]:
        rows = await self._payments._require_pool().fetch(
            """
            SELECT intent.settlement_intent_id
            FROM arena402.settlement_intents AS intent
            LEFT JOIN arena402.x402_settlement_attempts AS attempt
              ON attempt.settlement_intent_id = intent.settlement_intent_id
            WHERE intent.status = 'authorization_requested'
              AND (
                    $2::text IS NULL
                    OR intent.settlement_intent_id = $2::text
              )
              AND (
                    attempt.settlement_intent_id IS NULL
                    OR (
                        attempt.status IN ('reserved', 'signed')
                        AND attempt.lease_expires_at < clock_timestamp()
                    )
              )
            ORDER BY intent.created_at, intent.settlement_intent_id
            LIMIT $1
            """,
            limit,
            self._settlement_intent_id,
        )
        return [str(row["settlement_intent_id"]) for row in rows]

    async def unknown_submission_targets(self, *, limit: int) -> list[str]:
        rows = await self._payments._require_pool().fetch(
            """
            SELECT attempt.settlement_intent_id
            FROM arena402.x402_settlement_attempts AS attempt
            JOIN arena402.settlement_intents AS intent
              ON intent.settlement_intent_id = attempt.settlement_intent_id
            LEFT JOIN arena402.settlement_submissions AS submission
              ON submission.settlement_intent_id = attempt.settlement_intent_id
            WHERE intent.status = 'authorization_requested'
              AND (
                    $2::text IS NULL
                    OR intent.settlement_intent_id = $2::text
              )
              AND submission.settlement_intent_id IS NULL
              AND attempt.status IN ('submitting', 'unknown')
            ORDER BY attempt.updated_at, attempt.settlement_intent_id
            LIMIT $1
            """,
            limit,
            self._settlement_intent_id,
        )
        return [str(row["settlement_intent_id"]) for row in rows]

    async def record_recovered_submission(
        self,
        *,
        settlement_intent_id: str,
        tx_hash: str,
        now: datetime,
    ) -> None:
        row = await self._payments._require_pool().fetchrow(
            """
            SELECT reservation_id, facilitator_id
            FROM arena402.x402_settlement_attempts
            WHERE settlement_intent_id = $1
              AND status IN ('submitting', 'unknown')
            """,
            settlement_intent_id,
        )
        if row is None:
            return
        await self._payments.submit_reservation(
            str(row["reservation_id"]),
            tx_hash=tx_hash,
            now=now,
        )
        terms = await self.settlement_terms(settlement_intent_id)
        await self._arena.record_automatic_submission(
            settlement_intent_id=settlement_intent_id,
            tx_hash=tx_hash,
            authorization_nonce=(
                "0x" + terms.intent_hash.removeprefix("sha256:")
            ),
            approved_intent_hash=terms.intent_hash,
        )
        facilitator_id = row["facilitator_id"]
        if facilitator_id is not None:
            await self._payments._require_pool().execute(
                """
                DELETE FROM arena402.facilitator_broadcast_fences
                WHERE facilitator_id = $1
                  AND settlement_intent_id = $2
                """,
                str(facilitator_id),
                settlement_intent_id,
            )

    async def settlement_terms(self, settlement_intent_id: str) -> SettlementTerms:
        intent = await self._arena.settlement_intent_for_payment(
            settlement_intent_id=settlement_intent_id
        )
        if not intent.token_eip712_name or not intent.token_eip712_version:
            raise ValueError("token_eip712_domain_not_frozen")
        return SettlementTerms(
            settlement_intent_id=intent.settlement_intent_id,
            intent_hash=intent.intent_hash,
            game_id=intent.game_id,
            payer=intent.buyer_account,
            payee=intent.seller_account,
            chain_id=intent.chain_id,
            token_address=intent.token_address,
            token_symbol=intent.token_symbol,
            token_decimals=intent.token_decimals,
            token_eip712_name=intent.token_eip712_name,
            token_eip712_version=intent.token_eip712_version,
            amount_atomic=intent.amount_atomic,
            resource_url=(
                f"{self._public_api_url}/api/v1/x402/settlement-intents/"
                f"{intent.settlement_intent_id}/execute"
            ),
        )

    async def active_mandate(
        self, settlement_intent_id: str, now: datetime
    ) -> PaymentMandate | None:
        return await self._payments.active_mandate_for_settlement(
            settlement_intent_id=settlement_intent_id,
            now=now,
        )

    async def claim_attempt(
        self,
        *,
        settlement_intent_id: str,
        reservation_id: str,
        payment_required: dict,
        facilitator_id: str,
        worker_id: str,
        now: datetime,
    ) -> bool:
        lease_expires = now + timedelta(seconds=self._lease_seconds)
        row = await self._payments._require_pool().fetchrow(
            """
            INSERT INTO arena402.x402_settlement_attempts (
                settlement_intent_id, reservation_id, x402_version,
                network, payment_required, facilitator_id, status, lease_owner,
                lease_expires_at, created_at, updated_at
            )
            VALUES (
                $1, $2, 2, $3, $4::jsonb, $5, 'reserved', $6, $7, $8, $8
            )
            ON CONFLICT (settlement_intent_id) DO UPDATE
            SET facilitator_id = COALESCE(
                    x402_settlement_attempts.facilitator_id,
                    EXCLUDED.facilitator_id
                ),
                lease_owner = EXCLUDED.lease_owner,
                lease_expires_at = EXCLUDED.lease_expires_at,
                updated_at = EXCLUDED.updated_at
            WHERE x402_settlement_attempts.status IN ('reserved', 'signed')
              AND x402_settlement_attempts.lease_expires_at < $8
              AND (
                    x402_settlement_attempts.facilitator_id IS NULL
                    OR x402_settlement_attempts.facilitator_id
                        = EXCLUDED.facilitator_id
              )
            RETURNING settlement_intent_id
            """,
            settlement_intent_id,
            reservation_id,
            payment_required["accepts"][0]["network"],
            json.dumps(
                payment_required,
                sort_keys=True,
                separators=(",", ":"),
            ),
            facilitator_id,
            worker_id,
            lease_expires,
            now,
        )
        return row is not None

    async def mark_attempt(
        self,
        *,
        settlement_intent_id: str,
        status: str,
        worker_id: str,
        safe_error_code: str | None = None,
        payment_payload_digest: str | None = None,
    ) -> None:
        if status not in {
            "signed",
            "submitting",
            "submitted",
            "failed",
            "unknown",
        }:
            raise ValueError("invalid_x402_attempt_status")
        if status == "signed":
            if not isinstance(payment_payload_digest, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                payment_payload_digest,
            ):
                raise ValueError("invalid_payment_payload_digest")
        elif payment_payload_digest is not None:
            raise ValueError("payment_payload_digest_only_allowed_when_signed")
        terminal = status in {"submitted", "failed", "unknown"}
        updated = await self._payments._require_pool().execute(
            """
            UPDATE arena402.x402_settlement_attempts
            SET status = $3,
                safe_error_code = $4,
                payment_payload_digest = CASE
                    WHEN $3 = 'signed' THEN $5
                    ELSE payment_payload_digest
                END,
                lease_owner = CASE WHEN $6 THEN NULL ELSE lease_owner END,
                lease_expires_at = CASE
                    WHEN $6 THEN NULL ELSE lease_expires_at
                END,
                updated_at = clock_timestamp()
            WHERE settlement_intent_id = $1
              AND lease_owner = $2
            """,
            settlement_intent_id,
            worker_id,
            status,
            safe_error_code,
            payment_payload_digest,
            terminal,
        )
        if updated != "UPDATE 1":
            raise RuntimeError("automatic_payment_lease_lost")

    async def claim_facilitator_fence(
        self,
        *,
        facilitator_id: str,
        settlement_intent_id: str,
        worker_id: str,
        now: datetime,
    ) -> bool:
        lease_expires = now + timedelta(seconds=self._lease_seconds)
        row = await self._payments._require_pool().fetchrow(
            """
            INSERT INTO arena402.facilitator_broadcast_fences AS fence (
                facilitator_id, settlement_intent_id, lease_owner,
                lease_expires_at, created_at, updated_at
            )
            SELECT $1, $2, $3, $4, $5, $5
            WHERE NOT EXISTS (
                SELECT 1
                FROM arena402.x402_settlement_attempts AS attempt
                LEFT JOIN arena402.settlement_submissions AS submission
                  ON submission.settlement_intent_id
                    = attempt.settlement_intent_id
                WHERE attempt.facilitator_id = $1
                  AND attempt.status IN ('submitting', 'unknown')
                  AND submission.settlement_intent_id IS NULL
            )
            ON CONFLICT (facilitator_id) DO UPDATE
            SET settlement_intent_id = EXCLUDED.settlement_intent_id,
                lease_owner = EXCLUDED.lease_owner,
                lease_expires_at = EXCLUDED.lease_expires_at,
                updated_at = EXCLUDED.updated_at
            WHERE fence.lease_expires_at < $5
              AND NOT EXISTS (
                    SELECT 1
                    FROM arena402.x402_settlement_attempts AS attempt
                    LEFT JOIN arena402.settlement_submissions AS submission
                      ON submission.settlement_intent_id
                        = attempt.settlement_intent_id
                    WHERE attempt.facilitator_id = $1
                      AND attempt.status IN ('submitting', 'unknown')
                      AND submission.settlement_intent_id IS NULL
              )
            RETURNING settlement_intent_id
            """,
            facilitator_id,
            settlement_intent_id,
            worker_id,
            lease_expires,
            now,
        )
        return row is not None

    async def release_facilitator_fence(
        self,
        *,
        facilitator_id: str,
        settlement_intent_id: str,
        worker_id: str,
    ) -> None:
        deleted = await self._payments._require_pool().execute(
            """
            DELETE FROM arena402.facilitator_broadcast_fences
            WHERE facilitator_id = $1
              AND settlement_intent_id = $2
              AND lease_owner = $3
            """,
            facilitator_id,
            settlement_intent_id,
            worker_id,
        )
        if deleted != "DELETE 1":
            raise RuntimeError("facilitator_broadcast_fence_lost")

    async def fail_settlement(
        self,
        *,
        settlement_intent_id: str,
        safe_error_code: str,
    ) -> None:
        await self._arena.record_automatic_failure(
            settlement_intent_id=settlement_intent_id,
            safe_error_code=safe_error_code,
        )
