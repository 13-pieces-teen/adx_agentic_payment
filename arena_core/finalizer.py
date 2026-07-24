"""Deadline-driven deterministic task convergence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from .models import ArenaResultRecord
from .repository import ArenaCoreRepository


class ArenaDeadlineFinalizer:
    def __init__(
        self,
        repository: ArenaCoreRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def finalize_expired(self, *, limit: int = 100) -> list[ArenaResultRecord]:
        return await self._repository.finalize_expired(
            server_clock=self._clock,
            limit=limit,
        )
