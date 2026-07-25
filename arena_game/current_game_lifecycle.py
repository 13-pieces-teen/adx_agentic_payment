"""Lifecycle owner for the single public Arena 402 Current Game."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .event_deck import STANDARD_EVENT_DECK_ID, build_event_schedule
from .postgres import (
    CURRENT_GAME_MAX_PARTICIPANTS,
    PostgresPawnhouseRepository,
)
from .settlement import SettlementConfig


class CurrentGameLifecycleWorker:
    """Create the first Current Game and rotate it after terminal completion."""

    def __init__(
        self,
        *,
        repository: PostgresPawnhouseRepository,
        settlement_config: SettlementConfig,
        round_count: int = 5,
        start_threshold: int = 10,
        max_participants: int = CURRENT_GAME_MAX_PARTICIPANTS,
        action_timeout_ms: int = 90_000,
        max_negotiation_turns: int = 3,
    ) -> None:
        if round_count < 1:
            raise ValueError("round_count must be positive")
        if not 2 <= start_threshold <= CURRENT_GAME_MAX_PARTICIPANTS:
            raise ValueError(
                "start_threshold must be between 2 and "
                f"{CURRENT_GAME_MAX_PARTICIPANTS}"
            )
        if not (
            start_threshold
            <= max_participants
            <= CURRENT_GAME_MAX_PARTICIPANTS
        ):
            raise ValueError(
                "max_participants must be between start_threshold and "
                f"{CURRENT_GAME_MAX_PARTICIPANTS}"
            )
        if action_timeout_ms <= 0:
            raise ValueError("action_timeout_ms must be positive")
        if max_negotiation_turns not in {2, 3}:
            raise ValueError("max_negotiation_turns must be 2 or 3")

        self._repository = repository
        self._settlement_config = settlement_config
        self._round_count = round_count
        self._start_threshold = start_threshold
        self._max_participants = max_participants
        self._action_timeout_ms = action_timeout_ms
        self._max_negotiation_turns = max_negotiation_turns

    async def run_once(self) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        suffix = uuid.uuid4().hex[:12]
        game_id = f"game-{now:%Y%m%d-%H%M%S}-{suffix}"
        event_seed = f"current-game:{game_id}"
        events = build_event_schedule(
            round_count=self._round_count,
            seed=event_seed,
            deck_id=STANDARD_EVENT_DECK_ID,
            mode="seeded_shuffle",
        )
        return await self._repository.ensure_current_game(
            game_id=game_id,
            events=events,
            event_seed=event_seed,
            event_deck_id=STANDARD_EVENT_DECK_ID,
            event_mode="seeded_shuffle",
            action_timeout_ms=self._action_timeout_ms,
            max_negotiation_turns=self._max_negotiation_turns,
            start_threshold=self._start_threshold,
            max_participants=self._max_participants,
            settlement_config=self._settlement_config,
        )


__all__ = ["CurrentGameLifecycleWorker"]
