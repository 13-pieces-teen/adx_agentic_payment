"""Versioned deterministic event-deck scheduling."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Final, Literal

from .events import WorldEvent
from .presets import MVP_EVENT_CATALOG, demo_events


STANDARD_EVENT_DECK_ID: Final[str] = "pawnhouse-standard-v1"
EventMode = Literal["fixed_demo", "seeded_shuffle"]


class EventDeckError(ValueError):
    pass


def build_event_schedule(
    *,
    round_count: int,
    seed: str,
    deck_id: str = STANDARD_EVENT_DECK_ID,
    mode: EventMode = "fixed_demo",
) -> tuple[WorldEvent, ...]:
    """Build an immutable one-event-per-round schedule."""

    if not isinstance(round_count, int) or isinstance(round_count, bool):
        raise EventDeckError("round_count must be an integer")
    if round_count < 1 or round_count > len(MVP_EVENT_CATALOG):
        raise EventDeckError(
            f"round_count must be between 1 and {len(MVP_EVENT_CATALOG)}"
        )
    if not seed:
        raise EventDeckError("event seed is required")
    if deck_id != STANDARD_EVENT_DECK_ID:
        raise EventDeckError("unknown event deck")
    if mode == "fixed_demo":
        if round_count != 5:
            raise EventDeckError(
                "fixed_demo_requires_exactly_five_rounds"
            )
        return demo_events()
    if mode != "seeded_shuffle":
        raise EventDeckError("unknown event mode")

    ordered = sorted(
        MVP_EVENT_CATALOG.values(),
        key=lambda event: hashlib.sha256(
            (
                "arena.event-deck.v1\0"
                f"{deck_id}\0{seed}\0{event.event_id}"
            ).encode("utf-8")
        ).digest(),
    )
    return tuple(
        replace(event, reveal_round=round_index)
        for round_index, event in enumerate(
            ordered[:round_count],
            start=1,
        )
    )


__all__ = [
    "EventDeckError",
    "EventMode",
    "STANDARD_EVENT_DECK_ID",
    "build_event_schedule",
]
