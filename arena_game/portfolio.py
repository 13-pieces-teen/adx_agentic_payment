"""Initial allocation and terminal valuation rules."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Mapping, Sequence

from .goods import GOOD_IDS, GoodId, INITIAL_PRICES
from .money import gold


INITIAL_NET_WORTH_ATOMIC = gold("20")


class PortfolioError(ValueError):
    pass


def distribute_balanced_portfolios(
    agent_ids: Sequence[str],
    *,
    seed: str,
) -> dict[str, "Portfolio"]:
    """Give every participant equal net worth and a deterministic inventory seed.

    Each participant receives one deterministic good unit and the remainder in
    cash. This creates both buyer and seller liquidity without changing the
    fixed 20-gold starting valuation.
    """

    if len(set(agent_ids)) != len(agent_ids):
        raise PortfolioError("agent ids must be unique")
    portfolios: dict[str, Portfolio] = {}
    for index, agent_id in enumerate(agent_ids):
        digest = sha256(f"{seed}:{agent_id}:{index}".encode()).digest()
        good = GOOD_IDS[int.from_bytes(digest[:4], "big") % len(GOOD_IDS)]
        holdings = {good_id: 0 for good_id in GOOD_IDS}
        holdings[good] = 1
        portfolios[agent_id] = Portfolio.initial(
            cash_atomic=INITIAL_NET_WORTH_ATOMIC - INITIAL_PRICES[good],
            holdings=holdings,
        )
    return portfolios


def normalize_holdings(values: Mapping[str, int]) -> dict[GoodId, int]:
    unknown = set(values).difference(GOOD_IDS)
    if unknown:
        raise PortfolioError(f"unknown goods: {', '.join(sorted(unknown))}")
    normalized: dict[GoodId, int] = {}
    for good_id in GOOD_IDS:
        quantity = values.get(good_id, 0)
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise PortfolioError("holding quantities must be integers")
        if quantity < 0 or quantity > 1_000_000:
            raise PortfolioError("holding quantity is outside the Arena bound")
        normalized[good_id] = quantity
    return normalized


def portfolio_value(
    cash_atomic: int,
    holdings: Mapping[str, int],
    prices: Mapping[GoodId, int],
) -> int:
    if cash_atomic < 0:
        raise PortfolioError("cash cannot be negative")
    normalized = normalize_holdings(holdings)
    if set(prices) != set(GOOD_IDS):
        raise PortfolioError("valuation requires exactly the four canonical goods")
    return cash_atomic + sum(
        normalized[good_id] * prices[good_id] for good_id in GOOD_IDS
    )


@dataclass(frozen=True, slots=True)
class Portfolio:
    cash_atomic: int
    holdings: dict[GoodId, int]

    @classmethod
    def initial(
        cls,
        *,
        cash_atomic: int,
        holdings: Mapping[str, int],
    ) -> "Portfolio":
        normalized = normalize_holdings(holdings)
        value = portfolio_value(cash_atomic, normalized, INITIAL_PRICES)
        if value != INITIAL_NET_WORTH_ATOMIC:
            raise PortfolioError(
                "initial cash plus holdings must equal exactly 20 gold"
            )
        return cls(cash_atomic=cash_atomic, holdings=normalized)

    def net_worth(self, prices: Mapping[GoodId, int]) -> int:
        return portfolio_value(self.cash_atomic, self.holdings, prices)


__all__ = [
    "INITIAL_NET_WORTH_ATOMIC",
    "Portfolio",
    "PortfolioError",
    "distribute_balanced_portfolios",
    "normalize_holdings",
    "portfolio_value",
]
