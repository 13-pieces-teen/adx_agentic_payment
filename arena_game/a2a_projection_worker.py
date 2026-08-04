"""Durable projection of applied real-Agent market Results."""

from __future__ import annotations

from typing import Protocol

from arena_core.models import AppliedArenaAction


class AgentDrivenMarketProjectionRepository(Protocol):
    async def pending_agent_market_applications(
        self,
        *,
        limit: int,
    ) -> list[AppliedArenaAction]: ...

    async def project_agent_market_application(
        self,
        application: AppliedArenaAction,
    ) -> dict[str, object]: ...


class AgentDrivenMarketProjectionWorker:
    """Retry-safe bridge from Result Sink applications to market state."""

    def __init__(
        self,
        repository: AgentDrivenMarketProjectionRepository,
    ) -> None:
        self._repository = repository

    async def run_once(
        self,
        *,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        pending = await self._repository.pending_agent_market_applications(
            limit=min(limit, 1000)
        )
        projected: list[dict[str, object]] = []
        for application in pending:
            projected.append(
                await self._repository.project_agent_market_application(
                    application
                )
            )
        return projected


__all__ = [
    "AgentDrivenMarketProjectionRepository",
    "AgentDrivenMarketProjectionWorker",
]
