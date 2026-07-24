"""The four canonical goods traded in the King's Pawnhouse."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

from .money import gold


GoodId: TypeAlias = Literal["grain", "iron", "warhorse", "gems"]


@dataclass(frozen=True, slots=True)
class GoodDefinition:
    good_id: GoodId
    display_name: str
    icon: str
    initial_price_atomic: int
    financial_role: str


GOODS: Final[dict[GoodId, GoodDefinition]] = {
    "grain": GoodDefinition(
        good_id="grain",
        display_name="粮草",
        icon="🌾",
        initial_price_atomic=gold("2"),
        financial_role="民生必需品",
    ),
    "iron": GoodDefinition(
        good_id="iron",
        display_name="精铁",
        icon="⚔️",
        initial_price_atomic=gold("5"),
        financial_role="工业与战争周期品",
    ),
    "warhorse": GoodDefinition(
        good_id="warhorse",
        display_name="战马",
        icon="🐎",
        initial_price_atomic=gold("8"),
        financial_role="稀缺硬资产",
    ),
    "gems": GoodDefinition(
        good_id="gems",
        display_name="宝石",
        icon="💎",
        initial_price_atomic=gold("3"),
        financial_role="投机资产",
    ),
}

GOOD_IDS: Final[tuple[GoodId, ...]] = tuple(GOODS)
INITIAL_PRICES: Final[dict[GoodId, int]] = {
    good_id: definition.initial_price_atomic
    for good_id, definition in GOODS.items()
}


def require_good(value: str) -> GoodId:
    if value not in GOODS:
        raise ValueError(f"unknown King's Pawnhouse good: {value}")
    return value  # type: ignore[return-value]


__all__ = [
    "GOODS",
    "GOOD_IDS",
    "INITIAL_PRICES",
    "GoodDefinition",
    "GoodId",
    "require_good",
]
