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

    def __post_init__(self) -> None:
        if self.entered_at.tzinfo is None or self.entered_at.utcoffset() is None:
            raise MarketError("entered_at must include a timezone")


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


def fcfs_pair(entries: Iterable[PoolEntry]) -> tuple[Pairing, ...]:
    """Pair buyers and sellers by authoritative receive time, per good."""

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
        for index, (buyer, seller) in enumerate(
            zip(buyers, sellers, strict=False),
            start=1,
        ):
            if buyer.participant_id == seller.participant_id:
                raise MarketError("a participant cannot trade with itself")
            output.append(
                Pairing(
                    pairing_id=(
                        f"pair:{buyer.game_id}:{buyer.round_id}:{good}:{index}"
                    ),
                    game_id=buyer.game_id,
                    round_id=buyer.round_id,
                    good=good,
                    buyer_entry_id=buyer.pool_entry_id,
                    seller_entry_id=seller.pool_entry_id,
                    buyer_participant_id=buyer.participant_id,
                    seller_participant_id=seller.participant_id,
                    sequence=index,
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
