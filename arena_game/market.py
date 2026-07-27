"""Authoritative decision pool and first-come-first-served pairing rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Literal

from .goods import GoodId


MarketSide = Literal["buy", "sell"]


class MarketError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PoolEntry:
    pool_entry_id: str
    game_id: str
    round_id: str
    participant_id: str
    side: MarketSide
    good: GoodId
    entered_at: datetime
    quantity: int = 1
    limit_price_atomic: int | None = None

    def __post_init__(self) -> None:
        if self.entered_at.tzinfo is None or self.entered_at.utcoffset() is None:
            raise MarketError("entered_at must include a timezone")
        if self.quantity <= 0:
            raise MarketError("pool quantity must be positive")
        if self.limit_price_atomic is not None and self.limit_price_atomic <= 0:
            raise MarketError("pool limit price must be positive")


@dataclass(frozen=True, slots=True)
class Pairing:
    pairing_id: str
    game_id: str
    round_id: str
    good: GoodId
    buyer_entry_id: str
    seller_entry_id: str
    buyer_participant_id: str
    seller_participant_id: str
    sequence: int
    quantity: int = 1
    buyer_limit_price_atomic: int | None = None
    seller_limit_price_atomic: int | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise MarketError("pairing quantity must be positive")


def _limits_overlap(buyer: PoolEntry, seller: PoolEntry) -> bool:
    return (
        buyer.limit_price_atomic is None
        or seller.limit_price_atomic is None
        or buyer.limit_price_atomic >= seller.limit_price_atomic
    )


def fcfs_pair(entries: Iterable[PoolEntry]) -> tuple[Pairing, ...]:
    """Pair price-compatible buyers and sellers by receive time, per good."""

    materialized = tuple(entries)
    if len({entry.pool_entry_id for entry in materialized}) != len(materialized):
        raise MarketError("pool entry ids must be unique")
    if not materialized:
        return ()
    game_ids = {entry.game_id for entry in materialized}
    round_ids = {entry.round_id for entry in materialized}
    if len(game_ids) != 1 or len(round_ids) != 1:
        raise MarketError("FCFS pairing accepts one game round at a time")

    output: list[Pairing] = []
    for good in ("grain", "iron", "warhorse", "gems"):
        buyers = sorted(
            (
                entry
                for entry in materialized
                if entry.good == good and entry.side == "buy"
            ),
            key=lambda entry: (
                entry.entered_at,
                entry.pool_entry_id,
            ),
        )
        sellers = sorted(
            (
                entry
                for entry in materialized
                if entry.good == good and entry.side == "sell"
            ),
            key=lambda entry: (
                entry.entered_at,
                entry.pool_entry_id,
            ),
        )
        unmatched_sellers = list(sellers)
        sequence = 0
        for buyer in buyers:
            compatible_index = next(
                (
                    index
                    for index, seller in enumerate(unmatched_sellers)
                    if _limits_overlap(buyer, seller)
                ),
                None,
            )
            if compatible_index is None:
                continue
            seller = unmatched_sellers.pop(compatible_index)
            if buyer.participant_id == seller.participant_id:
                raise MarketError("a participant cannot trade with itself")
            sequence += 1
            output.append(
                Pairing(
                    pairing_id=(
                        f"pair:{buyer.game_id}:{buyer.round_id}:{good}:{sequence}"
                    ),
                    game_id=buyer.game_id,
                    round_id=buyer.round_id,
                    good=good,
                    buyer_entry_id=buyer.pool_entry_id,
                    seller_entry_id=seller.pool_entry_id,
                    buyer_participant_id=buyer.participant_id,
                    seller_participant_id=seller.participant_id,
                    sequence=sequence,
                    quantity=min(buyer.quantity, seller.quantity),
                    buyer_limit_price_atomic=buyer.limit_price_atomic,
                    seller_limit_price_atomic=seller.limit_price_atomic,
                )
            )
    return tuple(output)


__all__ = [
    "MarketError",
    "MarketSide",
    "Pairing",
    "PoolEntry",
    "fcfs_pair",
]
