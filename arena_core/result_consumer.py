"""Validate and apply durable Runtime results exactly once."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from .models import AppliedArenaAction, ArenaResultRecord
from .repository import ArenaCoreRepository


class ArenaResultConsumer:
    def __init__(
        self,
        repository: ArenaCoreRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def consume_pending(self, *, limit: int = 100) -> list[AppliedArenaAction]:
        records = await self._repository.pending_results(limit=limit)
        applied: list[AppliedArenaAction] = []
        for record in records:
            item = await self.consume(record)
            if item is not None:
                applied.append(item)
        return applied

    async def consume(
        self, record: ArenaResultRecord
    ) -> AppliedArenaAction | None:
        # The record is only a pointer. The repository re-reads and projects
        # its authoritative result inside the same CAS boundary.
        return await self._repository.apply_result(
            result_id=record.result.result_id,
            server_clock=self._clock,
        )
