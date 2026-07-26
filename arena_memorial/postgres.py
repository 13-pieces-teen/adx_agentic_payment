"""PostgreSQL-backed founding-memorial read model."""

from __future__ import annotations

from typing import Any

import asyncpg

from .models import MemorialAward, MemorialStats


class PostgresMemorialRepository:
    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("memorial_database_url_required")
        self.database_url = database_url
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.database_url,
                min_size=1,
                max_size=4,
                command_timeout=15,
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def reconcile(self) -> int:
        value = await self._require_pool().fetchval(
            "SELECT arena402.reconcile_memorial_awards($1)",
            "arena402-genesis",
        )
        return int(value or 0)

    async def award_for_user(self, user_id: str) -> MemorialAward | None:
        row = await self._require_pool().fetchrow(
            """
            SELECT
                award.campaign_id,
                award.user_id,
                award.registration_rank,
                award.token_id,
                award.wallet_id,
                award.wallet_address,
                award.registered_at,
                award.eligibility_status,
                award.mint_status,
                award.credential_status,
                campaign.contract_address,
                award.mint_tx_hash,
                award.mint_block_number,
                award.assigned_at,
                award.submitted_at,
                award.minted_at
            FROM arena402.memorial_awards AS award
            JOIN arena402.memorial_campaigns AS campaign
              ON campaign.campaign_id = award.campaign_id
            WHERE award.campaign_id = $1
              AND award.user_id = $2
            """,
            "arena402-genesis",
            user_id,
        )
        return _award(row) if row is not None else None

    async def stats(self) -> MemorialStats:
        row = await self._require_pool().fetchrow(
            """
            SELECT
                campaign.campaign_id,
                campaign.name,
                campaign.symbol,
                campaign.chain_id,
                campaign.contract_address,
                campaign.status AS campaign_status,
                campaign.max_supply,
                count(award.user_id)::INTEGER AS reserved_count,
                count(award.user_id) FILTER (
                    WHERE award.mint_status = 'submitted'
                )::INTEGER AS submitted_count,
                count(award.user_id) FILTER (
                    WHERE award.mint_status = 'minted'
                )::INTEGER AS minted_count
            FROM arena402.memorial_campaigns AS campaign
            LEFT JOIN arena402.memorial_awards AS award
              ON award.campaign_id = campaign.campaign_id
             AND award.eligibility_status = 'reserved'
            WHERE campaign.campaign_id = $1
            GROUP BY campaign.campaign_id
            """,
            "arena402-genesis",
        )
        if row is None:
            raise RuntimeError("memorial_campaign_not_found")
        return MemorialStats(
            campaign_id=str(row["campaign_id"]),
            name=str(row["name"]),
            symbol=str(row["symbol"]),
            chain_id=int(row["chain_id"]),
            contract_address=(
                str(row["contract_address"])
                if row["contract_address"] is not None
                else None
            ),
            campaign_status=str(row["campaign_status"]),
            max_supply=int(row["max_supply"]),
            reserved_count=int(row["reserved_count"]),
            submitted_count=int(row["submitted_count"]),
            minted_count=int(row["minted_count"]),
        )

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("memorial_repository_not_initialized")
        return self._pool


def _award(row: Any) -> MemorialAward:
    return MemorialAward(
        campaign_id=str(row["campaign_id"]),
        user_id=str(row["user_id"]),
        registration_rank=int(row["registration_rank"]),
        token_id=int(row["token_id"]),
        wallet_id=str(row["wallet_id"]),
        wallet_address=str(row["wallet_address"]),
        registered_at=row["registered_at"],
        eligibility_status=str(row["eligibility_status"]),
        mint_status=str(row["mint_status"]),
        credential_status=str(row["credential_status"]),
        contract_address=(
            str(row["contract_address"])
            if row["contract_address"] is not None
            else None
        ),
        mint_tx_hash=(
            str(row["mint_tx_hash"]) if row["mint_tx_hash"] is not None else None
        ),
        mint_block_number=(
            int(row["mint_block_number"])
            if row["mint_block_number"] is not None
            else None
        ),
        assigned_at=row["assigned_at"],
        submitted_at=row["submitted_at"],
        minted_at=row["minted_at"],
    )


__all__ = ["PostgresMemorialRepository"]
