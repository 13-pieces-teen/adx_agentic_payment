"""Versioned deterministic event-deck scheduling."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Final, Literal

from .events import WorldEvent
from .presets import (
    EXPERIMENTAL_EVENT_CATALOG_V2,
    MVP_EVENT_CATALOG,
    demo_events,
)


STANDARD_EVENT_DECK_ID: Final[str] = "pawnhouse-standard-v1"
EXPERIMENTAL_EVENT_DECK_ID_V2: Final[str] = "pawnhouse-standard-v2"
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
    catalogs = {
        STANDARD_EVENT_DECK_ID: MVP_EVENT_CATALOG,
        EXPERIMENTAL_EVENT_DECK_ID_V2: EXPERIMENTAL_EVENT_CATALOG_V2,
    }
    try:
        catalog = catalogs[deck_id]
    except KeyError:
        raise EventDeckError("unknown event deck") from None
    if round_count < 1 or round_count > len(catalog):
        raise EventDeckError(
            f"round_count must be between 1 and {len(catalog)}"
        )
    if not seed:
        raise EventDeckError("event seed is required")
    if mode == "fixed_demo":
        if deck_id != STANDARD_EVENT_DECK_ID:
            raise EventDeckError(
                "fixed_demo_requires_standard_v1_deck"
            )
        if round_count != 5:
            raise EventDeckError(
                "fixed_demo_requires_exactly_five_rounds"
            )
        return demo_events()
    if mode != "seeded_shuffle":
        raise EventDeckError("unknown event mode")

    ordered = sorted(
        catalog.values(),
        key=lambda event: hashlib.sha256(
            (
                (
                    "arena.event-deck.v1\0"
                    if deck_id == STANDARD_EVENT_DECK_ID
                    else "arena.event-deck.v2\0"
                )
                + f"{deck_id}\0{seed}\0{event.event_id}"
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
    "EXPERIMENTAL_EVENT_DECK_ID_V2",
    "STANDARD_EVENT_DECK_ID",
    "build_event_schedule",
]
