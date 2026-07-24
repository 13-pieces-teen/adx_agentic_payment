"""Deterministic fixed-point money helpers for the King's Pawnhouse."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Final


GOLD_SCALE: Final[int] = 1_000_000
MAX_GOLD_ATOMIC: Final[int] = 10**38 - 1


class MoneyError(ValueError):
    """Raised when a gold value is not canonical or exceeds the domain bound."""


def gold(value: str | int | Decimal) -> int:
    """Convert a gold-denominated value to atomic units.

    Floats are intentionally rejected. Values may have at most six decimal
    places and must be non-negative.
    """

    if isinstance(value, bool) or isinstance(value, float):
        raise MoneyError("gold values must not use binary floating point")
    if isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise MoneyError("invalid gold value") from exc
    else:
        raise MoneyError("gold value must be a string, integer, or Decimal")

    if not parsed.is_finite() or parsed < 0:
        raise MoneyError("gold value must be finite and non-negative")
    atomic = parsed * GOLD_SCALE
    if atomic != atomic.to_integral_value():
        raise MoneyError("gold value supports at most six decimal places")
    result = int(atomic)
    if result > MAX_GOLD_ATOMIC:
        raise MoneyError("gold value exceeds the Arena bound")
    return result


def format_gold(value_atomic: int) -> str:
    if isinstance(value_atomic, bool) or not isinstance(value_atomic, int):
        raise MoneyError("atomic gold must be an integer")
    if value_atomic < 0 or value_atomic > MAX_GOLD_ATOMIC:
        raise MoneyError("atomic gold is outside the Arena bound")
    whole, fraction = divmod(value_atomic, GOLD_SCALE)
    if fraction == 0:
        return str(whole)
    return f"{whole}.{fraction:06d}".rstrip("0")


def apply_basis_points(value_atomic: int, basis_points: int) -> int:
    """Apply a signed basis-point delta with deterministic half-up rounding."""

    if value_atomic < 0:
        raise MoneyError("cannot adjust a negative value")
    if basis_points < -10_000 or basis_points > 1_000_000:
        raise MoneyError("basis-point adjustment is outside the Arena bound")
    numerator = value_atomic * (10_000 + basis_points)
    result = (numerator + 5_000) // 10_000
    if result < 0 or result > MAX_GOLD_ATOMIC:
        raise MoneyError("adjusted gold value is outside the Arena bound")
    return result


__all__ = [
    "GOLD_SCALE",
    "MAX_GOLD_ATOMIC",
    "MoneyError",
    "apply_basis_points",
    "format_gold",
    "gold",
]
