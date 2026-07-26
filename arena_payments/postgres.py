"""PostgreSQL adapter for permanent wallet bindings and payment mandates."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from .models import (
    MandateLimits,
    PaymentMandate,
    PaymentReservation,
    SettlementTerms,
    UserWalletBinding,
)
from .repository import MandateRejected, WalletUnavailable, _identifier


def _binding(row: Any) -> UserWalletBinding:
    return UserWalletBinding(
        user_id=str(row["user_id"]),
        github_subject=str(row["github_subject"]),
        wallet_id=str(row["wallet_id"]),
        chain_id=int(row["chain_id"]),
        address=str(row["account_address"]),
        bound_at=row["bound_at"],
    )


def _mandate(row: Any) -> PaymentMandate:
    return PaymentMandate(
        mandate_id=str(row["mandate_id"]),
        user_id=str(row["user_id"]),
        wallet_id=str(row["wallet_id"]),
        game_id=str(row["game_id"]),
        chain_id=int(row["chain_id"]),
        token_address=str(row["token_address"]),
        limits=MandateLimits(
            max_per_payment_atomic=int(row["max_per_payment_atomic"]),
            max_cumulative_atomic=int(row["max_cumulative_atomic"]),
        ),
        allowed_payees=tuple(row["allowed_payees"]),
        valid_from=row["valid_from"],
        expires_at=row["expires_at"],
        reserved_atomic=int(row["reserved_atomic"]),
        consumed_atomic=int(row["consumed_atomic"]),
        revoked_at=row["revoked_at"],
        allowed_payee_rule=row.get("allowed_payee_rule"),
        join_authorization_id=row.get("join_authorization_id"),
    )


def _reservation(row: Any) -> PaymentReservation:
    return PaymentReservation(
        reservation_id=str(row["reservation_id"]),
        mandate_id=str(row["mandate_id"]),
        settlement_intent_id=str(row["settlement_intent_id"]),
        intent_hash=str(row["intent_hash"]),
        amount_atomic=int(row["amount_atomic"]),
        payee=str(row["payee"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        reserved_at=row["reserved_at"],
        finalized_at=row["finalized_at"],
        tx_hash=row["tx_hash"],
        release_reason=row["release_reason"],
    )


class PostgresPaymentRepository:
    """Serializes allocation/accounting in PostgreSQL for multi-instance clouds."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._pool: Any = None
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        async with self._initialize_lock:
            if self._pool is not None:
                return
            try:
                import asyncpg  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "asyncpg is required for PostgreSQL payment persistence"
                ) from exc
            self._pool = await asyncpg.create_pool(
                self.database_url,
                min_size=1,
                max_size=5,
                command_timeout=30,
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("Payment repository is not initialized")
        return self._pool

    async def get_or_bind_wallet(
        self,
        *,
        user_id: str,
        identity_provider: str,
        provider_subject: str | None,
        now: datetime,
    ) -> UserWalletBinding:
        del identity_provider, provider_subject
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                user = await connection.fetchrow(
                    """
                    SELECT user_id, identity_provider, provider_subject
                    FROM public.connector_users
                    WHERE user_id = $1
                    FOR UPDATE
                    """,
                    user_id,
                )
                if (
                    user is None
                    or user["identity_provider"] != "github"
                    or not user["provider_subject"]
                ):
                    raise WalletUnavailable("github_identity_required")
                existing = await connection.fetchrow(
                    """
                    SELECT user_id, github_subject, wallet_id, chain_id,
                           account_address, bound_at
                    FROM arena402.user_wallets
                    WHERE user_id = $1
                    """,
                    user_id,
                )
                if existing is not None:
                    if existing["github_subject"] != user["provider_subject"]:
                        raise WalletUnavailable("github_identity_conflict")
                    return _binding(existing)
                wallet = await connection.fetchrow(
                    """
                    SELECT wallet_id, chain_id, account_address
                    FROM arena402.wallet_inventory
                    WHERE status = 'available'
                    ORDER BY wallet_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """
                )
                if wallet is None:
                    raise WalletUnavailable("wallet_pool_exhausted")
                try:
                    created = await connection.fetchrow(
                        """
                        INSERT INTO arena402.user_wallets (
                            user_id, github_subject, wallet_id, chain_id,
                            account_address, bound_at
                        )
                        VALUES ($1, $2, $3, $4, $5, $6)
                        RETURNING user_id, github_subject, wallet_id, chain_id,
                                  account_address, bound_at
                        """,
                        user_id,
                        user["provider_subject"],
                        wallet["wallet_id"],
                        wallet["chain_id"],
                        wallet["account_address"],
                        now,
                    )
                except Exception as exc:
                    if getattr(exc, "sqlstate", None) == "23505":
                        raise WalletUnavailable("wallet_binding_conflict") from exc
                    raise
                await connection.execute(
                    """
                    UPDATE arena402.wallet_inventory
                    SET status = 'bound'
                    WHERE wallet_id = $1 AND status = 'available'
                    """,
                    wallet["wallet_id"],
                )
                return _binding(created)

    async def wallet_for_user(self, *, user_id: str) -> UserWalletBinding | None:
        row = await self._require_pool().fetchrow(
            """
            SELECT user_id, github_subject, wallet_id, chain_id,
                   account_address, bound_at
            FROM arena402.user_wallets
            WHERE user_id = $1
            """,
            user_id,
        )
        return _binding(row) if row else None

    async def create_mandate(self, mandate: PaymentMandate) -> PaymentMandate:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                if mandate.allowed_payee_rule is None:
                    context = await connection.fetchrow(
                        """
                        SELECT
                            wallet.chain_id AS wallet_chain_id,
                            game.config_snapshot -> 'settlement'
                                AS settlement_config
                        FROM arena402.game_participants AS participant
                        JOIN arena402.games AS game
                          ON game.game_id = participant.game_id
                        JOIN arena402.user_wallets AS wallet
                          ON wallet.user_id = participant.user_id
                         AND wallet.wallet_id = $3
                        WHERE participant.game_id = $1
                          AND participant.user_id = $2
                          AND participant.status IN (
                              'joined', 'active', 'settling'
                          )
                        """,
                        mandate.game_id,
                        mandate.user_id,
                        mandate.wallet_id,
                    )
                else:
                    context = await connection.fetchrow(
                        """
                        SELECT
                            wallet.chain_id AS wallet_chain_id,
                            game.config_snapshot -> 'settlement'
                                AS settlement_config
                        FROM arena402.join_authorizations AS authorization
                        JOIN arena402.games AS game
                          ON game.game_id = authorization.game_id
                        JOIN arena402.user_wallets AS wallet
                          ON wallet.user_id = authorization.user_id
                         AND wallet.wallet_id = $4
                        WHERE authorization.join_authorization_id = $1
                          AND authorization.game_id = $2
                          AND authorization.user_id = $3
                          AND authorization.status = 'pending'
                          AND authorization.expires_at > clock_timestamp()
                        FOR SHARE OF authorization, game, wallet
                        """,
                        mandate.join_authorization_id,
                        mandate.game_id,
                        mandate.user_id,
                        mandate.wallet_id,
                    )
                if context is None:
                    raise MandateRejected(
                        "join_authorization_required"
                        if mandate.allowed_payee_rule is not None
                        else "game_participation_required"
                    )
                if int(context["wallet_chain_id"]) != mandate.chain_id:
                    raise MandateRejected("mandate_wallet_chain_mismatch")
                raw_config = context["settlement_config"]
                settlement_config = (
                    json.loads(raw_config)
                    if isinstance(raw_config, str)
                    else dict(raw_config or {})
                )
                if settlement_config.get("authorizationMode") == "none":
                    raise MandateRejected("game_settlement_disabled")
                if (
                    int(settlement_config.get("chainId", 0))
                    != mandate.chain_id
                ):
                    raise MandateRejected("mandate_game_chain_mismatch")
                if (
                    str(settlement_config.get("tokenAddress", "")).lower()
                    != mandate.token_address
                ):
                    raise MandateRejected("mandate_game_token_mismatch")
                if mandate.allowed_payee_rule is None:
                    invalid_payee = await connection.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM unnest($2::text[]) AS requested(account_address)
                            WHERE NOT EXISTS (
                                SELECT 1
                                FROM arena402.participant_settlement_accounts AS account
                                WHERE account.game_id = $1
                                  AND account.account_address =
                                      requested.account_address
                            )
                        )
                        """,
                        mandate.game_id,
                        list(mandate.allowed_payees),
                    )
                    if invalid_payee:
                        raise MandateRejected("mandate_payee_not_in_game")
                await connection.execute(
                    """
                    UPDATE arena402.payment_mandates
                    SET revoked_at = clock_timestamp()
                    WHERE user_id = $1
                      AND game_id = $2
                      AND mandate_id <> $3
                      AND revoked_at IS NULL
                      AND expires_at <= clock_timestamp()
                    """,
                    mandate.user_id,
                    mandate.game_id,
                    mandate.mandate_id,
                )
                try:
                    row = await connection.fetchrow(
                        """
                        INSERT INTO arena402.payment_mandates (
                            mandate_id, user_id, wallet_id, game_id, chain_id,
                            token_address, max_per_payment_atomic,
                            max_cumulative_atomic, allowed_payees, valid_from,
                            expires_at, revoked_at, allowed_payee_rule,
                            join_authorization_id
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6,
                            $7, $8, $9, $10, $11, $12, $13, $14
                        )
                        ON CONFLICT (mandate_id) DO NOTHING
                        RETURNING *
                        """,
                        mandate.mandate_id,
                        mandate.user_id,
                        mandate.wallet_id,
                        mandate.game_id,
                        mandate.chain_id,
                        mandate.token_address,
                        mandate.limits.max_per_payment_atomic,
                        mandate.limits.max_cumulative_atomic,
                        list(mandate.allowed_payees),
                        mandate.valid_from,
                        mandate.expires_at,
                        mandate.revoked_at,
                        mandate.allowed_payee_rule,
                        mandate.join_authorization_id,
                    )
                except Exception as exc:
                    if getattr(exc, "sqlstate", None) == "23505":
                        raise MandateRejected("active_game_mandate_exists") from exc
                    raise
                if row is None:
                    row = await connection.fetchrow(
                        """
                        SELECT *
                        FROM arena402.payment_mandates
                        WHERE mandate_id = $1
                        """,
                        mandate.mandate_id,
                    )
                    if row is None or _mandate(row) != mandate:
                        raise MandateRejected("mandate_conflict")
                return _mandate(row)

    async def active_mandate(
        self, *, user_id: str, game_id: str, now: datetime
    ) -> PaymentMandate | None:
        row = await self._require_pool().fetchrow(
            """
            SELECT *
            FROM arena402.payment_mandates
            WHERE user_id = $1
              AND game_id = $2
              AND revoked_at IS NULL
              AND valid_from <= $3
              AND expires_at > $3
            """,
            user_id,
            game_id,
            now,
        )
        return _mandate(row) if row else None

    async def active_mandate_for_settlement(
        self,
        *,
        settlement_intent_id: str,
        now: datetime,
    ) -> PaymentMandate | None:
        row = await self._require_pool().fetchrow(
            """
            SELECT mandate.*
            FROM arena402.settlement_intents AS intent
            JOIN arena402.game_participants AS buyer
              ON buyer.game_participant_id = intent.buyer_participant_id
             AND buyer.game_id = intent.game_id
            JOIN arena402.payment_mandates AS mandate
              ON mandate.user_id = buyer.user_id
             AND mandate.game_id = intent.game_id
             AND mandate.chain_id = intent.chain_id
             AND mandate.token_address = intent.token_address
            JOIN arena402.user_wallets AS wallet
              ON wallet.user_id = mandate.user_id
             AND wallet.wallet_id = mandate.wallet_id
             AND wallet.account_address = intent.buyer_account
            LEFT JOIN arena402.payment_reservations AS reservation
              ON reservation.settlement_intent_id =
                 intent.settlement_intent_id
             AND reservation.mandate_id = mandate.mandate_id
            WHERE intent.settlement_intent_id = $1
              AND (
                    reservation.status IN ('reserved', 'submitted')
                    OR (
                        mandate.revoked_at IS NULL
                        AND mandate.valid_from <= $2
                        AND mandate.expires_at > $2
                    )
              )
            """,
            settlement_intent_id,
            now,
        )
        return _mandate(row) if row else None

    async def reserve_mandate(
        self,
        *,
        mandate_id: str,
        terms: SettlementTerms,
        now: datetime,
    ) -> PaymentReservation:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                existing = await connection.fetchrow(
                    """
                    SELECT *
                    FROM arena402.payment_reservations
                    WHERE settlement_intent_id = $1
                    """,
                    terms.settlement_intent_id,
                )
                if existing is not None:
                    value = _reservation(existing)
                    if (
                        value.mandate_id != mandate_id
                        or value.intent_hash != terms.intent_hash
                        or value.amount_atomic != terms.amount_atomic
                        or value.payee != terms.payee
                    ):
                        raise MandateRejected("reservation_intent_conflict")
                    return value
                mandate_row = await connection.fetchrow(
                    """
                    SELECT mandate.*, wallet.account_address
                    FROM arena402.payment_mandates AS mandate
                    JOIN arena402.user_wallets AS wallet
                      ON wallet.user_id = mandate.user_id
                     AND wallet.wallet_id = mandate.wallet_id
                    WHERE mandate.mandate_id = $1
                    FOR UPDATE OF mandate
                    """,
                    mandate_id,
                )
                if mandate_row is None:
                    raise MandateRejected("mandate_not_found")
                intent = await connection.fetchrow(
                    """
                    SELECT intent_hash, game_id, round_id,
                           buyer_participant_id, buyer_account, seller_account,
                           chain_id, token_address, amount_atomic
                    FROM arena402.settlement_intents
                    WHERE settlement_intent_id = $1
                    """,
                    terms.settlement_intent_id,
                )
                if intent is None or not self._intent_matches(intent, terms):
                    raise MandateRejected("frozen_intent_mismatch")
                if mandate_row["account_address"] != terms.payer:
                    raise MandateRejected("mandate_payer_mismatch")
                buyer_cash = await connection.fetchval(
                    """
                    SELECT cash_atomic
                    FROM arena402.balances
                    WHERE game_participant_id = $1
                    """,
                    intent["buyer_participant_id"],
                )
                if buyer_cash is None:
                    raise MandateRejected("buyer_balance_not_found")
                outstanding = await connection.fetchval(
                    """
                    SELECT COALESCE(sum(amount_atomic), 0)
                    FROM arena402.payment_reservations
                    WHERE buyer_participant_id = $1
                      AND status IN ('reserved', 'submitted')
                    """,
                    intent["buyer_participant_id"],
                )
                if int(outstanding) + terms.amount_atomic > int(buyer_cash):
                    raise MandateRejected("buyer_cash_reservation_limit")
                payee_allowed = terms.payee in mandate_row["allowed_payees"]
                if (
                    mandate_row.get("allowed_payee_rule")
                    == "same_game_settlement_account"
                ):
                    payee_allowed = bool(
                        await connection.fetchval(
                            """
                            SELECT EXISTS (
                                SELECT 1
                                FROM arena402.participant_settlement_accounts
                                    AS account
                                JOIN arena402.game_participants AS participant
                                  ON participant.game_participant_id =
                                     account.game_participant_id
                                 AND participant.game_id = account.game_id
                                WHERE account.game_id = $1
                                  AND account.account_address = $2
                                  AND participant.readiness = 'ready'
                                  AND participant.status IN ('active', 'settling')
                            )
                            """,
                            terms.game_id,
                            terms.payee,
                        )
                    )
                self._validate_mandate_row(
                    mandate_row,
                    terms,
                    now,
                    payee_allowed=payee_allowed,
                )
                reservation_id = _identifier(
                    {
                        "kind": "arena402.payment-reservation.v1",
                        "mandateId": mandate_id,
                        "settlementIntentId": terms.settlement_intent_id,
                        "intentHash": terms.intent_hash,
                    }
                )
                row = await connection.fetchrow(
                    """
                    INSERT INTO arena402.payment_reservations (
                        reservation_id, mandate_id, settlement_intent_id,
                        game_id, round_id, buyer_participant_id,
                        intent_hash, amount_atomic, payee, reserved_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    RETURNING *
                    """,
                    reservation_id,
                    mandate_id,
                    terms.settlement_intent_id,
                    intent["game_id"],
                    intent["round_id"],
                    intent["buyer_participant_id"],
                    terms.intent_hash,
                    terms.amount_atomic,
                    terms.payee,
                    now,
                )
                await connection.execute(
                    """
                    UPDATE arena402.payment_mandates
                    SET reserved_atomic = reserved_atomic + $2
                    WHERE mandate_id = $1
                    """,
                    mandate_id,
                    terms.amount_atomic,
                )
                return _reservation(row)

    async def consume_reservation(
        self,
        reservation_id: str,
        *,
        tx_hash: str,
        now: datetime,
    ) -> PaymentReservation:
        return await self._finalize_reservation(
            reservation_id,
            action="consume",
            value=tx_hash.lower(),
            now=now,
        )

    async def submit_reservation(
        self,
        reservation_id: str,
        *,
        tx_hash: str,
        now: datetime,
    ) -> PaymentReservation:
        del now
        normalized_tx = tx_hash.lower()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT *
                    FROM arena402.payment_reservations
                    WHERE reservation_id = $1
                    FOR UPDATE
                    """,
                    reservation_id,
                )
                if row is None:
                    raise MandateRejected("reservation_not_found")
                current = _reservation(row)
                if current.status in {"submitted", "consumed"}:
                    if current.tx_hash != normalized_tx:
                        raise MandateRejected("reservation_tx_conflict")
                    return current
                if current.status != "reserved":
                    raise MandateRejected("reservation_not_submittable")
                updated = await connection.fetchrow(
                    """
                    UPDATE arena402.payment_reservations
                    SET status = 'submitted', tx_hash = $2
                    WHERE reservation_id = $1
                    RETURNING *
                    """,
                    reservation_id,
                    normalized_tx,
                )
                return _reservation(updated)

    async def reconcile_finalized_reservations(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> int:
        """Finalize submitted accounting only after Arena has chain evidence."""
        if not 1 <= limit <= 500:
            raise ValueError("reservation_reconcile_limit_out_of_range")
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                rows = await connection.fetch(
                    """
                    SELECT reservation.reservation_id,
                           reservation.mandate_id,
                           reservation.amount_atomic,
                           intent.status
                    FROM arena402.payment_reservations AS reservation
                    JOIN arena402.settlement_intents AS intent
                      ON intent.settlement_intent_id =
                         reservation.settlement_intent_id
                    WHERE reservation.status = 'submitted'
                      AND intent.status IN (
                          'chain_confirmed_uncommitted',
                          'inventory_committed',
                          'reverted'
                      )
                    ORDER BY reservation.reserved_at,
                             reservation.reservation_id
                    FOR UPDATE OF reservation SKIP LOCKED
                    LIMIT $1
                    """,
                    limit,
                )
                for row in rows:
                    if row["status"] == "reverted":
                        await connection.execute(
                            """
                            UPDATE arena402.payment_reservations
                            SET status = 'released',
                                tx_hash = NULL,
                                release_reason = 'chain_transaction_reverted',
                                finalized_at = $2
                            WHERE reservation_id = $1
                            """,
                            row["reservation_id"],
                            now,
                        )
                        await connection.execute(
                            """
                            UPDATE arena402.payment_mandates
                            SET reserved_atomic = reserved_atomic - $2
                            WHERE mandate_id = $1
                            """,
                            row["mandate_id"],
                            row["amount_atomic"],
                        )
                    else:
                        await connection.execute(
                            """
                            UPDATE arena402.payment_reservations
                            SET status = 'consumed', finalized_at = $2
                            WHERE reservation_id = $1
                            """,
                            row["reservation_id"],
                            now,
                        )
                        await connection.execute(
                            """
                            UPDATE arena402.payment_mandates
                            SET reserved_atomic = reserved_atomic - $2,
                                consumed_atomic = consumed_atomic + $2
                            WHERE mandate_id = $1
                            """,
                            row["mandate_id"],
                            row["amount_atomic"],
                        )
                return len(rows)

    async def release_reservation(
        self,
        reservation_id: str,
        *,
        reason: str,
        now: datetime,
    ) -> PaymentReservation:
        if not reason or len(reason) > 100:
            raise MandateRejected("invalid_release_reason")
        return await self._finalize_reservation(
            reservation_id,
            action="release",
            value=reason,
            now=now,
        )

    async def revoke_mandate(
        self, *, mandate_id: str, user_id: str, now: datetime
    ) -> PaymentMandate:
        row = await self._require_pool().fetchrow(
            """
            UPDATE arena402.payment_mandates
            SET revoked_at = COALESCE(revoked_at, $3)
            WHERE mandate_id = $1 AND user_id = $2
            RETURNING *
            """,
            mandate_id,
            user_id,
            now,
        )
        if row is None:
            raise MandateRejected("mandate_not_found")
        return _mandate(row)

    async def admin_snapshot(self, *, limit: int = 200) -> dict[str, object]:
        if not 1 <= limit <= 500:
            raise ValueError("admin_snapshot_limit_out_of_range")
        pool = self._require_pool()
        wallet_rows = await pool.fetch(
            """
            SELECT inventory.wallet_id, inventory.chain_id,
                   inventory.account_address, inventory.status,
                   binding.user_id, binding.github_subject,
                   binding.bound_at
            FROM arena402.wallet_inventory AS inventory
            LEFT JOIN arena402.user_wallets AS binding
              ON binding.wallet_id = inventory.wallet_id
            ORDER BY inventory.wallet_id
            LIMIT $1
            """,
            limit,
        )
        mandate_rows = await pool.fetch(
            """
            SELECT mandate_id, user_id, wallet_id, game_id, chain_id,
                   token_address, max_per_payment_atomic,
                   max_cumulative_atomic, reserved_atomic, consumed_atomic,
                   valid_from, expires_at, revoked_at
            FROM arena402.payment_mandates
            ORDER BY created_at DESC, mandate_id DESC
            LIMIT $1
            """,
            limit,
        )
        attempt_rows = await pool.fetch(
            """
            SELECT attempt.settlement_intent_id, intent.game_id,
                   attempt.reservation_id, attempt.network,
                   attempt.facilitator_id, attempt.status,
                   attempt.safe_error_code, attempt.updated_at,
                   submission.tx_hash
            FROM arena402.x402_settlement_attempts AS attempt
            JOIN arena402.settlement_intents AS intent
              ON intent.settlement_intent_id =
                 attempt.settlement_intent_id
            LEFT JOIN arena402.settlement_submissions AS submission
              ON submission.settlement_intent_id =
                 attempt.settlement_intent_id
            ORDER BY attempt.updated_at DESC,
                     attempt.settlement_intent_id DESC
            LIMIT $1
            """,
            limit,
        )
        counts = await pool.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM arena402.wallet_inventory) AS wallets,
                (SELECT count(*) FROM arena402.user_wallets) AS bindings,
                (SELECT count(*) FROM arena402.payment_mandates
                    WHERE revoked_at IS NULL
                      AND expires_at > clock_timestamp()) AS active_mandates,
                (SELECT count(*) FROM arena402.payment_reservations
                    WHERE status IN ('reserved', 'submitted'))
                    AS reserved_payments,
                (SELECT count(*) FROM arena402.settlement_intents
                    WHERE status = 'submitted') AS submitted_payments
            """
        )
        return {
            "counts": {key: int(value) for key, value in dict(counts).items()},
            "wallets": [
                {
                    "walletId": row["wallet_id"],
                    "chainId": int(row["chain_id"]),
                    "address": row["account_address"],
                    "status": row["status"],
                    "userId": row["user_id"],
                    "githubSubject": row["github_subject"],
                    "boundAt": (
                        row["bound_at"].isoformat()
                        if row["bound_at"] is not None
                        else None
                    ),
                }
                for row in wallet_rows
            ],
            "mandates": [
                {
                    "mandateId": row["mandate_id"],
                    "userId": row["user_id"],
                    "walletId": row["wallet_id"],
                    "gameId": row["game_id"],
                    "chainId": int(row["chain_id"]),
                    "tokenAddress": row["token_address"],
                    "maxPerPaymentAtomic": str(int(row["max_per_payment_atomic"])),
                    "maxCumulativeAtomic": str(int(row["max_cumulative_atomic"])),
                    "reservedAtomic": str(int(row["reserved_atomic"])),
                    "consumedAtomic": str(int(row["consumed_atomic"])),
                    "validFrom": row["valid_from"].isoformat(),
                    "expiresAt": row["expires_at"].isoformat(),
                    "revokedAt": (
                        row["revoked_at"].isoformat()
                        if row["revoked_at"] is not None
                        else None
                    ),
                }
                for row in mandate_rows
            ],
            "settlements": [
                {
                    "settlementIntentId": row["settlement_intent_id"],
                    "gameId": row["game_id"],
                    "reservationId": row["reservation_id"],
                    "network": row["network"],
                    "facilitatorId": row["facilitator_id"],
                    "status": row["status"],
                    "safeErrorCode": row["safe_error_code"],
                    "txHash": row["tx_hash"],
                    "updatedAt": row["updated_at"].isoformat(),
                }
                for row in attempt_rows
            ],
        }

    async def _finalize_reservation(
        self,
        reservation_id: str,
        *,
        action: str,
        value: str,
        now: datetime,
    ) -> PaymentReservation:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT *
                    FROM arena402.payment_reservations
                    WHERE reservation_id = $1
                    FOR UPDATE
                    """,
                    reservation_id,
                )
                if row is None:
                    raise MandateRejected("reservation_not_found")
                current = _reservation(row)
                expected_status = "consumed" if action == "consume" else "released"
                if current.status == expected_status:
                    current_value = (
                        current.tx_hash
                        if action == "consume"
                        else current.release_reason
                    )
                    if current_value != value:
                        raise MandateRejected(
                            "reservation_tx_conflict"
                            if action == "consume"
                            else "reservation_release_conflict"
                        )
                    return current
                allowed = (
                    {"reserved", "submitted"}
                    if action == "consume"
                    else {"reserved"}
                )
                if current.status not in allowed:
                    raise MandateRejected(
                        "reservation_not_consumable"
                        if action == "consume"
                        else "reservation_not_releasable"
                    )
                await connection.fetchrow(
                    """
                    SELECT mandate_id
                    FROM arena402.payment_mandates
                    WHERE mandate_id = $1
                    FOR UPDATE
                    """,
                    current.mandate_id,
                )
                if action == "consume":
                    updated = await connection.fetchrow(
                        """
                        UPDATE arena402.payment_reservations
                        SET status = 'consumed', finalized_at = $2, tx_hash = $3
                        WHERE reservation_id = $1
                        RETURNING *
                        """,
                        reservation_id,
                        now,
                        value,
                    )
                    await connection.execute(
                        """
                        UPDATE arena402.payment_mandates
                        SET reserved_atomic = reserved_atomic - $2,
                            consumed_atomic = consumed_atomic + $2
                        WHERE mandate_id = $1
                        """,
                        current.mandate_id,
                        current.amount_atomic,
                    )
                else:
                    updated = await connection.fetchrow(
                        """
                        UPDATE arena402.payment_reservations
                        SET status = 'released', finalized_at = $2,
                            release_reason = $3
                        WHERE reservation_id = $1
                        RETURNING *
                        """,
                        reservation_id,
                        now,
                        value,
                    )
                    await connection.execute(
                        """
                        UPDATE arena402.payment_mandates
                        SET reserved_atomic = reserved_atomic - $2
                        WHERE mandate_id = $1
                        """,
                        current.mandate_id,
                        current.amount_atomic,
                    )
                return _reservation(updated)

    @staticmethod
    def _intent_matches(row: Any, terms: SettlementTerms) -> bool:
        return (
            row["intent_hash"] == terms.intent_hash
            and row["game_id"] == terms.game_id
            and row["buyer_account"] == terms.payer
            and row["seller_account"] == terms.payee
            and int(row["chain_id"]) == terms.chain_id
            and row["token_address"] == terms.token_address
            and int(row["amount_atomic"]) == terms.amount_atomic
        )

    @staticmethod
    def _validate_mandate_row(
        row: Any,
        terms: SettlementTerms,
        now: datetime,
        *,
        payee_allowed: bool,
    ) -> None:
        if row["revoked_at"] is not None:
            raise MandateRejected("mandate_revoked")
        if now < row["valid_from"]:
            raise MandateRejected("mandate_not_yet_valid")
        if now >= row["expires_at"]:
            raise MandateRejected("mandate_expired")
        if row["game_id"] != terms.game_id:
            raise MandateRejected("mandate_game_mismatch")
        if int(row["chain_id"]) != terms.chain_id:
            raise MandateRejected("mandate_chain_mismatch")
        if row["token_address"] != terms.token_address:
            raise MandateRejected("mandate_token_mismatch")
        if not payee_allowed:
            raise MandateRejected("mandate_payee_not_allowed")
        if terms.amount_atomic > int(row["max_per_payment_atomic"]):
            raise MandateRejected("mandate_per_payment_limit")
        if int(row["reserved_atomic"]) + int(
            row["consumed_atomic"]
        ) + terms.amount_atomic > int(row["max_cumulative_atomic"]):
            raise MandateRejected("mandate_cumulative_limit")
