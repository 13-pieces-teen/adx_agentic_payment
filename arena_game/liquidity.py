"""Privacy-safe round liquidity summaries for the Agent-driven market."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from .goods import GOOD_IDS, GoodId, require_good


MarketSide = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class LiquidityIntent:
    participant_id: str
    side: MarketSide
    good: GoodId
    limit_price_atomic: int

    def __post_init__(self) -> None:
        if not self.participant_id:
            raise ValueError("participant_id is required")
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        require_good(self.good)
        if (
            isinstance(self.limit_price_atomic, bool)
            or not isinstance(self.limit_price_atomic, int)
            or self.limit_price_atomic <= 0
        ):
            raise ValueError("limit_price_atomic must be a positive integer")


@dataclass(frozen=True, slots=True)
class GoodLiquiditySummary:
    buy_intent_count: int
    sell_intent_count: int
    opposite_side_capacity: int
    price_compatible_capacity: int

    def to_public_payload(self) -> dict[str, int]:
        return {
            "buyIntentCount": self.buy_intent_count,
            "sellIntentCount": self.sell_intent_count,
            "oppositeSideCapacity": self.opposite_side_capacity,
            "priceCompatibleCapacity": self.price_compatible_capacity,
        }


@dataclass(frozen=True, slots=True)
class RoundLiquiditySummary:
    participant_count: int
    intent_count: int
    pass_count: int
    opposite_side_capacity: int
    price_compatible_capacity: int
    by_good: dict[GoodId, GoodLiquiditySummary]

    def to_public_payload(self) -> dict[str, object]:
        return {
            "schemaVersion": "arena.market-liquidity.v1",
            "participantCount": self.participant_count,
            "intentCount": self.intent_count,
            "passCount": self.pass_count,
            "oppositeSideCapacity": self.opposite_side_capacity,
            "priceCompatibleCapacity": self.price_compatible_capacity,
            "priceCompatibilityGap": (
                self.opposite_side_capacity
                - self.price_compatible_capacity
            ),
            "minimumUnmatchedIntentCount": (
                self.intent_count - 2 * self.price_compatible_capacity
            ),
            "byGood": {
                good: self.by_good[good].to_public_payload()
                for good in GOOD_IDS
            },
        }


def _compatible_capacity(
    buyer_limits: Sequence[int],
    seller_limits: Sequence[int],
) -> int:
    buyers = sorted(buyer_limits)
    sellers = sorted(seller_limits)
    seller_index = 0
    matches = 0
    for buyer_limit in buyers:
        if (
            seller_index < len(sellers)
            and buyer_limit >= sellers[seller_index]
        ):
            matches += 1
            seller_index += 1
    return matches


def summarize_round_liquidity(
    *,
    participant_count: int,
    intents: Sequence[LiquidityIntent],
) -> RoundLiquiditySummary:
    if (
        isinstance(participant_count, bool)
        or not isinstance(participant_count, int)
        or participant_count < 0
    ):
        raise ValueError("participant_count must be a non-negative integer")
    participant_ids = [intent.participant_id for intent in intents]
    if len(set(participant_ids)) != len(participant_ids):
        raise ValueError("a participant may publish at most one intent")
    if len(intents) > participant_count:
        raise ValueError("intent count cannot exceed participant count")

    by_good: dict[GoodId, GoodLiquiditySummary] = {}
    for good in GOOD_IDS:
        buyer_limits = [
            intent.limit_price_atomic
            for intent in intents
            if intent.good == good and intent.side == "buy"
        ]
        seller_limits = [
            intent.limit_price_atomic
            for intent in intents
            if intent.good == good and intent.side == "sell"
        ]
        by_good[good] = GoodLiquiditySummary(
            buy_intent_count=len(buyer_limits),
            sell_intent_count=len(seller_limits),
            opposite_side_capacity=min(
                len(buyer_limits),
                len(seller_limits),
            ),
            price_compatible_capacity=_compatible_capacity(
                buyer_limits,
                seller_limits,
            ),
        )

    return RoundLiquiditySummary(
        participant_count=participant_count,
        intent_count=len(intents),
        pass_count=participant_count - len(intents),
        opposite_side_capacity=sum(
            summary.opposite_side_capacity
            for summary in by_good.values()
        ),
        price_compatible_capacity=sum(
            summary.price_compatible_capacity
            for summary in by_good.values()
        ),
        by_good=by_good,
    )


__all__ = [
    "GoodLiquiditySummary",
    "LiquidityIntent",
    "MarketSide",
    "RoundLiquiditySummary",
    "summarize_round_liquidity",
]
