"""Initial allocation and terminal valuation rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .goods import GOOD_IDS, GoodId, INITIAL_PRICES
from .money import gold


INITIAL_NET_WORTH_ATOMIC = gold("20")


class PortfolioError(ValueError):
    pass


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
    "normalize_holdings",
    "portfolio_value",
]
