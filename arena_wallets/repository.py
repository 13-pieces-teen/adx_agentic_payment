"""Persistence for user-controlled wallet bindings and settlement activity."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .crypto import normalize_address


class WalletRepositoryError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ExternalWalletBinding:
    user_id: str
    chain_id: int
    address: str
    verified_at: datetime


@dataclass(frozen=True, slots=True)
class WalletChallenge:
    challenge_id: str
    user_id: str
    chain_id: int
    address: str
    message_digest: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None


def digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _binding(row: Any) -> ExternalWalletBinding:
    return ExternalWalletBinding(
        user_id=str(row["user_id"]),
        chain_id=int(row["chain_id"]),
        address=normalize_address(str(row["account_address"])),
        verified_at=row["verified_at"],
    )


def _challenge(row: Any) -> WalletChallenge:
    return WalletChallenge(
        challenge_id=str(row["challenge_id"]),
        user_id=str(row["user_id"]),
        chain_id=int(row["chain_id"]),
        address=normalize_address(str(row["account_address"])),
        message_digest=str(row["message_digest"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        consumed_at=row.get("consumed_at"),
    )


class PostgresWalletRepository:
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
                raise RuntimeError("asyncpg is required for wallet persistence") from exc
            self._pool = await asyncpg.create_pool(
                self.database_url, min_size=1, max_size=5, command_timeout=30
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("Wallet repository is not initialized")
        return self._pool

    async def create_challenge(
        self,
        *,
        challenge_id: str,
        user_id: str,
        chain_id: int,
        address: str,
        message_digest: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> WalletChallenge:
        row = await self._require_pool().fetchrow(
            """
            INSERT INTO arena402.wallet_binding_challenges (
                challenge_id, user_id, chain_id, account_address,
                message_digest, created_at, expires_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING challenge_id, user_id, chain_id, account_address,
                      message_digest, created_at, expires_at, consumed_at
            """,
            challenge_id,
            user_id,
            chain_id,
            normalize_address(address),
            message_digest,
            created_at,
            expires_at,
        )
        return _challenge(row)

    async def get_challenge(self, *, challenge_id: str, user_id: str) -> WalletChallenge | None:
        row = await self._require_pool().fetchrow(
            """
            SELECT challenge_id, user_id, chain_id, account_address,
                   message_digest, created_at, expires_at, consumed_at
            FROM arena402.wallet_binding_challenges
            WHERE challenge_id = $1 AND user_id = $2
            """,
            challenge_id,
            user_id,
        )
        return _challenge(row) if row else None

    async def consume_and_bind(
        self,
        *,
        challenge_id: str,
        user_id: str,
        verified_at: datetime,
    ) -> ExternalWalletBinding:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                challenge = await connection.fetchrow(
                    """
                    SELECT challenge_id, user_id, chain_id, account_address,
                           message_digest, created_at, expires_at, consumed_at
                    FROM arena402.wallet_binding_challenges
                    WHERE challenge_id = $1 AND user_id = $2
                    FOR UPDATE
                    """,
                    challenge_id,
                    user_id,
                )
                if challenge is None:
                    raise WalletRepositoryError("wallet_challenge_not_found")
                if (
                    challenge["consumed_at"] is not None
                    or challenge["expires_at"] <= verified_at
                ):
                    raise WalletRepositoryError("wallet_challenge_expired")
                await connection.execute(
                    """
                    UPDATE arena402.wallet_binding_challenges
                    SET consumed_at = $2, verified_at = $2,
                        verification_status = 'verified'
                    WHERE challenge_id = $1
                    """,
                    challenge_id,
                    verified_at,
                )
                existing = await connection.fetchrow(
                    """
                    SELECT user_id, chain_id, account_address, verified_at
                    FROM arena402.external_wallet_bindings
                    WHERE user_id = $1
                    FOR UPDATE
                    """,
                    user_id,
                )
                address = normalize_address(str(challenge["account_address"]))
                if existing is not None:
                    existing_address = normalize_address(str(existing["account_address"]))
                    if existing_address != address or int(existing["chain_id"]) != int(challenge["chain_id"]):
                        raise WalletRepositoryError("wallet_already_bound")
                    return _binding(existing)
                try:
                    row = await connection.fetchrow(
                        """
                        INSERT INTO arena402.external_wallet_bindings (
                            user_id, chain_id, account_address, verified_at
                        )
                        VALUES ($1, $2, $3, $4)
                        RETURNING user_id, chain_id, account_address, verified_at
                        """,
                        user_id,
                        int(challenge["chain_id"]),
                        address,
                        verified_at,
                    )
                except Exception as exc:
                    if getattr(exc, "sqlstate", None) == "23505":
                        raise WalletRepositoryError("wallet_address_already_bound") from exc
                    raise
                return _binding(row)

    async def record_failed_verification(
        self, *, challenge_id: str, user_id: str, verified_at: datetime, code: str
    ) -> None:
        await self._require_pool().execute(
            """
            UPDATE arena402.wallet_binding_challenges
            SET consumed_at = $3, verified_at = $3,
                verification_status = 'failed', failure_code = $4
            WHERE challenge_id = $1 AND user_id = $2
              AND consumed_at IS NULL
            """,
            challenge_id,
            user_id,
            verified_at,
            code,
        )

    async def wallet_for_user(self, *, user_id: str) -> ExternalWalletBinding | None:
        row = await self._require_pool().fetchrow(
            """
            SELECT user_id, chain_id, account_address, verified_at
            FROM arena402.external_wallet_bindings
            WHERE user_id = $1
            """,
            user_id,
        )
        return _binding(row) if row else None

    async def unbind_wallet(self, *, user_id: str) -> bool:
        result = await self._require_pool().execute(
            "DELETE FROM arena402.external_wallet_bindings WHERE user_id = $1",
            user_id,
        )
        return result.endswith("1")

    async def activity_for_address(self, *, address: str, limit: int) -> list[dict[str, object]]:
        rows = await self._require_pool().fetch(
            """
            SELECT
                i.settlement_intent_id, i.game_id, i.round_id,
                r.round_index, i.good_id, good.display_name AS good_name,
                i.quantity, i.unit_price_atomic, i.amount_atomic,
                i.chain_id, i.token_address, i.token_symbol, i.token_decimals,
                i.buyer_account, i.seller_account,
                buyer.user_id AS buyer_user_id, buyer_user.username AS buyer_username,
                seller.user_id AS seller_user_id, seller_user.username AS seller_username,
                i.status, i.created_at, i.chain_confirmed_at,
                submission.tx_hash, confirmation.block_number,
                confirmation.observed_at AS confirmation_observed_at
            FROM arena402.settlement_intents AS i
            JOIN arena402.rounds AS r
              ON r.round_id = i.round_id AND r.game_id = i.game_id
            JOIN arena402.game_goods AS good
              ON good.game_id = i.game_id AND good.good_id = i.good_id
            JOIN arena402.game_participants AS buyer
              ON buyer.game_participant_id = i.buyer_participant_id
             AND buyer.game_id = i.game_id
            JOIN public.connector_users AS buyer_user
              ON buyer_user.user_id = buyer.user_id
            JOIN arena402.game_participants AS seller
              ON seller.game_participant_id = i.seller_participant_id
             AND seller.game_id = i.game_id
            JOIN public.connector_users AS seller_user
              ON seller_user.user_id = seller.user_id
            LEFT JOIN arena402.settlement_submissions AS submission
              ON submission.settlement_intent_id = i.settlement_intent_id
            LEFT JOIN arena402.settlement_confirmations AS confirmation
              ON confirmation.settlement_intent_id = i.settlement_intent_id
            WHERE i.buyer_account = $1 OR i.seller_account = $1
            ORDER BY i.created_at DESC, i.settlement_intent_id DESC
            LIMIT $2
            """,
            normalize_address(address),
            limit,
        )
        return [dict(row) for row in rows]

    async def transaction_for_address(
        self, *, address: str, tx_hash: str
    ) -> dict[str, object] | None:
        rows = await self._require_pool().fetch(
            """
            SELECT
                i.settlement_intent_id, i.game_id, i.round_id,
                r.round_index, i.good_id, good.display_name AS good_name,
                i.quantity, i.unit_price_atomic, i.amount_atomic,
                i.chain_id, i.token_address, i.token_symbol, i.token_decimals,
                i.buyer_account, i.seller_account,
                buyer.user_id AS buyer_user_id, buyer_user.username AS buyer_username,
                seller.user_id AS seller_user_id, seller_user.username AS seller_username,
                i.status, i.created_at, i.chain_confirmed_at,
                submission.tx_hash, confirmation.block_number,
                confirmation.observed_at AS confirmation_observed_at
            FROM arena402.settlement_submissions AS submission
            JOIN arena402.settlement_intents AS i
              ON i.settlement_intent_id = submission.settlement_intent_id
            JOIN arena402.rounds AS r
              ON r.round_id = i.round_id AND r.game_id = i.game_id
            JOIN arena402.game_goods AS good
              ON good.game_id = i.game_id AND good.good_id = i.good_id
            JOIN arena402.game_participants AS buyer
              ON buyer.game_participant_id = i.buyer_participant_id
             AND buyer.game_id = i.game_id
            JOIN public.connector_users AS buyer_user
              ON buyer_user.user_id = buyer.user_id
            JOIN arena402.game_participants AS seller
              ON seller.game_participant_id = i.seller_participant_id
             AND seller.game_id = i.game_id
            JOIN public.connector_users AS seller_user
              ON seller_user.user_id = seller.user_id
            LEFT JOIN arena402.settlement_confirmations AS confirmation
              ON confirmation.settlement_intent_id = i.settlement_intent_id
            WHERE submission.tx_hash = $1
              AND (i.buyer_account = $2 OR i.seller_account = $2)
            LIMIT 1
            """,
            tx_hash.lower(),
            normalize_address(address),
        )
        return dict(rows[0]) if rows else None


class MemoryWalletRepository:
    """Minimal repository used by unit tests and local API composition."""

    def __init__(self) -> None:
        self.challenges: dict[str, WalletChallenge] = {}
        self.bindings: dict[str, ExternalWalletBinding] = {}
        self.activity: list[dict[str, object]] = []

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def create_challenge(self, **kwargs: object) -> WalletChallenge:
        value = WalletChallenge(**kwargs)  # type: ignore[arg-type]
        self.challenges[value.challenge_id] = value
        return value

    async def get_challenge(self, *, challenge_id: str, user_id: str) -> WalletChallenge | None:
        value = self.challenges.get(challenge_id)
        return value if value and value.user_id == user_id else None

    async def consume_and_bind(self, *, challenge_id: str, user_id: str, verified_at: datetime) -> ExternalWalletBinding:
        challenge = self.challenges.get(challenge_id)
        if challenge is None:
            raise WalletRepositoryError("wallet_challenge_not_found")
        if challenge.consumed_at is not None or challenge.expires_at <= verified_at:
            raise WalletRepositoryError("wallet_challenge_expired")
        self.challenges[challenge_id] = WalletChallenge(
            challenge.challenge_id,
            challenge.user_id,
            challenge.chain_id,
            challenge.address,
            challenge.message_digest,
            challenge.created_at,
            challenge.expires_at,
            verified_at,
        )
        existing = self.bindings.get(user_id)
        address = challenge.address
        if existing:
            if existing.address != address or existing.chain_id != challenge.chain_id:
                raise WalletRepositoryError("wallet_already_bound")
            return existing
        if any(value.address == address and value.chain_id == challenge.chain_id for value in self.bindings.values()):
            raise WalletRepositoryError("wallet_address_already_bound")
        value = ExternalWalletBinding(user_id, challenge.chain_id, address, verified_at)
        self.bindings[user_id] = value
        return value

    async def record_failed_verification(self, **kwargs: object) -> None:
        challenge = self.challenges.get(str(kwargs["challenge_id"]))
        if challenge is not None and challenge.consumed_at is None:
            self.challenges[challenge.challenge_id] = WalletChallenge(
                challenge.challenge_id, challenge.user_id, challenge.chain_id,
                challenge.address, challenge.message_digest, challenge.created_at,
                challenge.expires_at, kwargs["verified_at"],  # type: ignore[arg-type]
            )

    async def wallet_for_user(self, *, user_id: str) -> ExternalWalletBinding | None:
        return self.bindings.get(user_id)

    async def unbind_wallet(self, *, user_id: str) -> bool:
        return self.bindings.pop(user_id, None) is not None

    async def activity_for_address(self, *, address: str, limit: int) -> list[dict[str, object]]:
        return self.activity[:limit]

    async def transaction_for_address(self, *, address: str, tx_hash: str) -> dict[str, object] | None:
        return next((item for item in self.activity if item.get("tx_hash") == tx_hash.lower()), None)
