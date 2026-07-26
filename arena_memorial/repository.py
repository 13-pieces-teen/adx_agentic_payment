"""Memorial repository contracts and an in-memory adapter for API tests."""

from __future__ import annotations

import asyncio
import copy
from typing import Protocol

from .models import MemorialAward, MemorialStats


class MemorialRepository(Protocol):
    async def reconcile(self) -> int: ...

    async def award_for_user(self, user_id: str) -> MemorialAward | None: ...

    async def stats(self) -> MemorialStats: ...


class InMemoryMemorialRepository:
    def __init__(
        self,
        *,
        awards: tuple[MemorialAward, ...] = (),
        stats: MemorialStats | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._awards = {award.user_id: award for award in awards}
        self._stats = stats or MemorialStats(
            campaign_id="arena402-genesis",
            name="Arena 402 Memorial",
            symbol="arena402",
            chain_id=1439,
            contract_address=None,
            campaign_status="preparing",
            max_supply=402,
            reserved_count=len(awards),
            submitted_count=sum(
                award.mint_status == "submitted" for award in awards
            ),
            minted_count=sum(award.mint_status == "minted" for award in awards),
        )

    async def reconcile(self) -> int:
        return 0

    async def award_for_user(self, user_id: str) -> MemorialAward | None:
        async with self._lock:
            return copy.deepcopy(self._awards.get(user_id))

    async def stats(self) -> MemorialStats:
        async with self._lock:
            return copy.deepcopy(self._stats)


__all__ = ["InMemoryMemorialRepository", "MemorialRepository"]
