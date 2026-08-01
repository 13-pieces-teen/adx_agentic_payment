"""Durable, backend-only orchestration for complete Pawnhouse games."""

from __future__ import annotations

import asyncio
import logging
from typing import Any


_LOGGER = logging.getLogger(__name__)


class PawnhouseGameOrchestrator:
    """Advance running games by one idempotent transition per poll cycle."""

    def __init__(self, *, repository: Any) -> None:
        self._repository = repository
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run_once(self, *, limit: int = 50) -> int:
        processed = 0
        actions = await self._repository.actionable_game_actions(limit=limit)
        for state in actions:
            game_id = str(state["gameId"])
            action = state.get("action")
            if action == "enqueue_agent_runtime":
                await self._repository.enqueue_agent_runtime_run(
                    game_id=game_id
                )
            elif action == "run_rule":
                await self._repository.run_rule_market(game_id=game_id)
            elif action == "advance_round":
                await self._repository.advance_round_or_game(game_id=game_id)
            else:
                continue
            processed += 1
        return processed

    async def run_forever(self, *, poll_seconds: float = 0.25) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        while not self._stopping.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.error("pawnhouse_game_orchestrator_cycle_failed")
                processed = 0
            if processed:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=poll_seconds,
                )
            except TimeoutError:
                pass


__all__ = ["PawnhouseGameOrchestrator"]
