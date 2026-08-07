"""Versioned, deterministic event engine for Aurelia's collapsing economy."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Iterable, Literal, Mapping

from .goods import GOOD_IDS, GoodId, INITIAL_PRICES, require_good
from .money import apply_basis_points


_EVENT_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
MARKET_FEEDBACK_POLICY_VERSION_V1 = "arena.market-feedback.v1"


class EventError(ValueError):
    pass


class EffectKind(str, Enum):
    PRICE_MULTIPLY_BPS = "price_multiply_bps"
    PRICE_RESET_TO_BASE = "price_reset_to_base"
    SUPPLY_INDEX_ADD_BPS = "supply_index_add_bps"
    BUBBLE_ADD_BPS = "bubble_add_bps"
    BUBBLE_CLEAR = "bubble_clear"
    CREATE_ROYAL_ORDER = "create_royal_order"


PriceTarget = Literal["market", "final", "both"]


@dataclass(frozen=True, slots=True)
class EventEffect:
    kind: EffectKind
    good: GoodId
    target: PriceTarget = "market"
    basis_points: int | None = None
    order_price_atomic: int | None = None
    order_limit: int | None = None

    def __post_init__(self) -> None:
        require_good(self.good)
        if self.target not in ("market", "final", "both"):
            raise EventError("invalid event effect target")
        needs_bps = self.kind in {
            EffectKind.PRICE_MULTIPLY_BPS,
            EffectKind.SUPPLY_INDEX_ADD_BPS,
            EffectKind.BUBBLE_ADD_BPS,
        }
        if needs_bps and self.basis_points is None:
            raise EventError(f"{self.kind.value} requires basis_points")
        if not needs_bps and self.basis_points is not None:
            raise EventError(f"{self.kind.value} does not accept basis_points")
        if self.kind is EffectKind.CREATE_ROYAL_ORDER:
            if (
                self.order_price_atomic is None
                or self.order_price_atomic <= 0
                or self.order_limit is None
                or self.order_limit <= 0
            ):
                raise EventError(
                    "create_royal_order requires a positive price and limit"
                )
        elif self.order_price_atomic is not None or self.order_limit is not None:
            raise EventError("only create_royal_order accepts order fields")

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": self.kind.value,
            "good": self.good,
            "target": self.target,
        }
        if self.basis_points is not None:
            result["basisPoints"] = self.basis_points
        if self.order_price_atomic is not None:
            result["orderPriceAtomic"] = str(self.order_price_atomic)
        if self.order_limit is not None:
            result["orderLimit"] = self.order_limit
        return result


@dataclass(frozen=True, slots=True)
class WorldEvent:
    event_id: str
    display_name: str
    narrative: str
    reveal_round: int
    duration_rounds: int | None
    effects: tuple[EventEffect, ...]
    schema_version: str = "arena.world-event.v1"

    def __post_init__(self) -> None:
        if not _EVENT_ID.fullmatch(self.event_id):
            raise EventError("event_id must be a lowercase kebab identifier")
        if not self.display_name.strip() or len(self.display_name) > 100:
            raise EventError("event display name is required")
        if len(self.narrative) > 1_000:
            raise EventError("event narrative is too long")
        if self.reveal_round < 1:
            raise EventError("reveal_round must be positive")
        if self.duration_rounds is not None and self.duration_rounds < 1:
            raise EventError("duration_rounds must be positive or null")
        if not self.effects:
            raise EventError("an event must contain at least one effect")

    def active_in_round(self, round_index: int) -> bool:
        if round_index < self.reveal_round:
            return False
        if self.duration_rounds is None:
            return True
        return round_index < self.reveal_round + self.duration_rounds

    def to_wire(self) -> dict[str, object]:
        return {
            "eventId": self.event_id,
            "displayName": self.display_name,
            "narrative": self.narrative,
            "revealRound": self.reveal_round,
            "durationRounds": self.duration_rounds,
            "effects": [effect.to_wire() for effect in self.effects],
            "schemaVersion": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class RoyalOrder:
    event_id: str
    good: GoodId
    price_atomic: int
    quantity_limit: int


@dataclass(frozen=True, slots=True)
class WorldSnapshot:
    round_index: int
    market_prices: dict[GoodId, int]
    final_prices: dict[GoodId, int]
    supply_index_bps: dict[GoodId, int]
    bubble_premium_bps: dict[GoodId, int]
    royal_orders: tuple[RoyalOrder, ...]
    revealed_event_ids: tuple[str, ...]


@dataclass(slots=True)
class WorldState:
    event_catalog: Mapping[str, WorldEvent]
    base_prices: dict[GoodId, int] = field(
        default_factory=lambda: dict(INITIAL_PRICES)
    )
    revealed_event_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if set(self.base_prices) != set(GOOD_IDS):
            raise EventError("base prices require exactly the canonical goods")
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            for value in self.base_prices.values()
        ):
            raise EventError("base prices must be positive integers")
        self.base_prices = dict(self.base_prices)

    def reveal(self, event_id: str, *, round_index: int) -> WorldSnapshot:
        event = self.event_catalog.get(event_id)
        if event is None:
            raise EventError(f"unknown event: {event_id}")
        if event.reveal_round != round_index:
            raise EventError("event cannot be revealed outside its scheduled round")
        if event_id in self.revealed_event_ids:
            raise EventError("event was already revealed")
        self.revealed_event_ids.append(event_id)
        return self.snapshot(round_index)

    def snapshot(self, round_index: int) -> WorldSnapshot:
        if round_index < 0:
            raise EventError("round_index cannot be negative")
        market = dict(self.base_prices)
        final = dict(self.base_prices)
        supply = {good_id: 10_000 for good_id in GOOD_IDS}
        bubble = {good_id: 0 for good_id in GOOD_IDS}
        orders: list[RoyalOrder] = []

        revealed = [
            self.event_catalog[event_id]
            for event_id in self.revealed_event_ids
            if self.event_catalog[event_id].reveal_round <= round_index
        ]
        for event in revealed:
            if event.active_in_round(round_index):
                for effect in event.effects:
                    self._apply_market_effect(
                        event.event_id,
                        effect,
                        market,
                        supply,
                        bubble,
                        orders,
                        self.base_prices,
                    )
            for effect in event.effects:
                self._apply_final_effect(
                    effect,
                    final,
                    self.base_prices,
                )

        return WorldSnapshot(
            round_index=round_index,
            market_prices=market,
            final_prices=final,
            supply_index_bps=supply,
            bubble_premium_bps=bubble,
            royal_orders=tuple(orders),
            revealed_event_ids=tuple(self.revealed_event_ids),
        )

    @staticmethod
    def _apply_market_effect(
        event_id: str,
        effect: EventEffect,
        market: dict[GoodId, int],
        supply: dict[GoodId, int],
        bubble: dict[GoodId, int],
        orders: list[RoyalOrder],
        base_prices: Mapping[GoodId, int],
    ) -> None:
        if effect.target not in ("market", "both"):
            return
        if effect.kind is EffectKind.PRICE_MULTIPLY_BPS:
            market[effect.good] = apply_basis_points(
                market[effect.good], effect.basis_points or 0
            )
        elif effect.kind is EffectKind.PRICE_RESET_TO_BASE:
            market[effect.good] = base_prices[effect.good]
        elif effect.kind is EffectKind.SUPPLY_INDEX_ADD_BPS:
            supply[effect.good] = max(
                0, supply[effect.good] + (effect.basis_points or 0)
            )
        elif effect.kind is EffectKind.BUBBLE_ADD_BPS:
            bubble[effect.good] += effect.basis_points or 0
        elif effect.kind is EffectKind.BUBBLE_CLEAR:
            bubble[effect.good] = 0
        elif effect.kind is EffectKind.CREATE_ROYAL_ORDER:
            assert effect.order_price_atomic is not None
            assert effect.order_limit is not None
            orders.append(
                RoyalOrder(
                    event_id=event_id,
                    good=effect.good,
                    price_atomic=effect.order_price_atomic,
                    quantity_limit=effect.order_limit,
                )
            )

    @staticmethod
    def _apply_final_effect(
        effect: EventEffect,
        final: dict[GoodId, int],
        base_prices: Mapping[GoodId, int],
    ) -> None:
        if effect.target not in ("final", "both"):
            return
        if effect.kind is EffectKind.PRICE_MULTIPLY_BPS:
            final[effect.good] = apply_basis_points(
                final[effect.good], effect.basis_points or 0
            )
        elif effect.kind is EffectKind.PRICE_RESET_TO_BASE:
            final[effect.good] = base_prices[effect.good]


def apply_market_feedback(
    snapshot: WorldSnapshot,
    *,
    last_clearing_prices: Mapping[GoodId, int],
    buy_pressure_bps: Mapping[GoodId, int],
) -> WorldSnapshot:
    """Apply bounded endogenous pressure to an event-derived snapshot.

    Events remain the fundamental anchor.  A previous clearing price contributes
    at most 25 percent of the next reference price, while order-flow pressure
    contributes at most 2 percent.  All arithmetic stays in integer basis
    points so replay remains deterministic.
    """

    market = dict(snapshot.market_prices)
    for good in GOOD_IDS:
        event_price = market[good]
        last_price = last_clearing_prices.get(good)
        pressure = max(-10_000, min(10_000, buy_pressure_bps.get(good, 0)))
        if last_price is not None and last_price > 0:
            event_price = (event_price * 3 + last_price) // 4
        pressure_effect = max(-2_000, min(2_000, pressure // 5))
        market[good] = max(1, apply_basis_points(event_price, pressure_effect))

    return replace(snapshot, market_prices=market)


def schedule_commitment(
    events: Iterable[WorldEvent],
    *,
    seed: str,
) -> str:
    if not seed:
        raise EventError("event seed is required")
    payload = {
        "seed": seed,
        "events": [event.to_wire() for event in events],
        "schemaVersion": "arena.event-schedule.v1",
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


__all__ = [
    "EffectKind",
    "EventEffect",
    "EventError",
    "MARKET_FEEDBACK_POLICY_VERSION_V1",
    "PriceTarget",
    "RoyalOrder",
    "WorldEvent",
    "WorldSnapshot",
    "WorldState",
    "apply_market_feedback",
    "schedule_commitment",
]
