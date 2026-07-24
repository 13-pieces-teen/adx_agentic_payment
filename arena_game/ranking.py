"""Terminal net-worth ranking for pawn promotion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .goods import GoodId
from .portfolio import Portfolio


@dataclass(frozen=True, slots=True)
class RankingEntry:
    rank: int
    agent_id: str
    net_worth_atomic: int
    tier: str


def promotion_tier(rank: int, participant_count: int) -> str:
    if rank == 1:
        return "公爵"
    percentile = rank / participant_count
    if percentile <= 0.25:
        return "御用商人"
    if percentile <= 0.6:
        return "王城行商"
    return "流浪商贩"


def calculate_rankings(
    portfolios: Mapping[str, Portfolio],
    final_prices: Mapping[GoodId, int],
) -> tuple[RankingEntry, ...]:
    if not portfolios:
        return ()
    ordered = sorted(
        (
            (agent_id, portfolio.net_worth(final_prices))
            for agent_id, portfolio in portfolios.items()
        ),
        key=lambda item: (-item[1], item[0]),
    )
    count = len(ordered)
    return tuple(
        RankingEntry(
            rank=index,
            agent_id=agent_id,
            net_worth_atomic=net_worth,
            tier=promotion_tier(index, count),
        )
        for index, (agent_id, net_worth) in enumerate(ordered, start=1)
    )


__all__ = ["RankingEntry", "calculate_rankings", "promotion_tier"]
