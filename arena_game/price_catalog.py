"""Versioned base-price catalogs frozen into Arena Game configuration."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping

from .goods import GOOD_IDS, GoodId, INITIAL_PRICES


STANDARD_PRICE_CATALOG_ID: Final = "pawnhouse-price-v1"
EXPERIMENTAL_PRICE_CATALOG_ID_V2: Final = "pawnhouse-price-v2"


@dataclass(frozen=True, slots=True)
class PriceCatalog:
    catalog_id: str
    prices: Mapping[GoodId, int]

    def __post_init__(self) -> None:
        if not self.catalog_id:
            raise ValueError("price catalog id is required")
        if set(self.prices) != set(GOOD_IDS):
            raise ValueError(
                "price catalog requires exactly the canonical goods"
            )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            for value in self.prices.values()
        ):
            raise ValueError("price catalog values must be positive integers")
        object.__setattr__(
            self,
            "prices",
            MappingProxyType(dict(self.prices)),
        )

    def to_snapshot(self) -> dict[str, object]:
        return {
            "priceCatalogId": self.catalog_id,
            "initialPricesAtomic": {
                good: str(self.prices[good]) for good in GOOD_IDS
            },
        }


_PRICE_CATALOGS: Final[dict[str, PriceCatalog]] = {
    STANDARD_PRICE_CATALOG_ID: PriceCatalog(
        catalog_id=STANDARD_PRICE_CATALOG_ID,
        prices=INITIAL_PRICES,
    ),
    EXPERIMENTAL_PRICE_CATALOG_ID_V2: PriceCatalog(
        catalog_id=EXPERIMENTAL_PRICE_CATALOG_ID_V2,
        prices={
            "grain": 2_500_000,
            "iron": 4_000_000,
            "warhorse": 6_000_000,
            "gems": 3_000_000,
        },
    ),
}


def resolve_price_catalog(catalog_id: str) -> PriceCatalog:
    try:
        return _PRICE_CATALOGS[catalog_id]
    except KeyError:
        raise ValueError("unknown price catalog") from None


def price_catalog_from_snapshot(
    snapshot: Mapping[str, object],
) -> PriceCatalog:
    catalog_id = snapshot.get("priceCatalogId")
    raw_prices = snapshot.get("initialPricesAtomic")
    if catalog_id is None and raw_prices is None:
        return resolve_price_catalog(STANDARD_PRICE_CATALOG_ID)
    if not isinstance(catalog_id, str) or not catalog_id:
        raise ValueError("frozen price catalog id is invalid")
    if not isinstance(raw_prices, Mapping):
        raise ValueError("frozen initial prices are invalid")
    if set(raw_prices) != set(GOOD_IDS):
        raise ValueError(
            "frozen initial prices require exactly the canonical goods"
        )
    prices: dict[GoodId, int] = {}
    for good in GOOD_IDS:
        raw_value = raw_prices[good]
        if isinstance(raw_value, bool) or not isinstance(
            raw_value,
            (str, int),
        ):
            raise ValueError("frozen initial price is invalid")
        try:
            value = int(raw_value)
        except ValueError:
            raise ValueError("frozen initial price is invalid") from None
        prices[good] = value
    return PriceCatalog(catalog_id=catalog_id, prices=prices)


__all__ = [
    "EXPERIMENTAL_PRICE_CATALOG_ID_V2",
    "PriceCatalog",
    "STANDARD_PRICE_CATALOG_ID",
    "price_catalog_from_snapshot",
    "resolve_price_catalog",
]
