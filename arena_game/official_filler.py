"""Server-authoritative Current Game official Agent backfill."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class OfficialFillerRepository(Protocol):
    async def official_fill_plan(
        self,
        *,
        now: datetime,
    ) -> dict[str, object]: ...

    async def add_official_hosted_participant(
        self,
        *,
        game_id: str,
        agent_id: str,
    ) -> str: ...


class OfficialAgentFiller:
    """Fill seats only after the repository's authoritative deadline."""

    def __init__(self, *, repository: OfficialFillerRepository) -> None:
        self._repository = repository

    async def run_once(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, object]:
        observed_at = now or datetime.now(timezone.utc)
        plan = await self._repository.official_fill_plan(now=observed_at)
        status = str(plan.get("status", "IDLE"))
        result = {**plan, "filledCount": 0}
        if status != "FILLING":
            return result

        game_id = str(plan["gameId"])
        candidate_ids = plan.get("candidateAgentIds", [])
        if not isinstance(candidate_ids, list):
            raise TypeError("candidateAgentIds must be a list")

        filled_count = 0
        for value in candidate_ids:
            agent_id = str(value)
            await self._repository.add_official_hosted_participant(
                game_id=game_id,
                agent_id=agent_id,
            )
            filled_count += 1
        result["filledCount"] = filled_count
        return result


__all__ = ["OfficialAgentFiller", "OfficialFillerRepository"]
