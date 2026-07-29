"""Validated PostgreSQL pool budgets shared by API-side repositories."""

from __future__ import annotations

import os


def api_pool_max_size() -> int:
    raw = os.getenv("ADX_API_DB_POOL_MAX_SIZE", "3").strip()
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(
            "ADX_API_DB_POOL_MAX_SIZE must be an integer"
        ) from None
    if value < 1 or value > 16:
        raise RuntimeError(
            "ADX_API_DB_POOL_MAX_SIZE must be between 1 and 16"
        )
    return value


__all__ = ["api_pool_max_size"]
