"""Versioned strategy archetypes for durable Hosted Arena Agents.

The archetype is a stable, public comparison label. Numeric variants and
learned revisions remain private Runtime configuration. Arena never treats a
strategy label as permission to bypass candidate validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


STRATEGY_CATALOG_VERSION_V1: Final = "arena.hosted-strategy.v1"


class StrategyArchetype(str, Enum):
    AGGRESSIVE = "aggressive"
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class StrategyPreset:
    archetype: StrategyArchetype
    public_label: str
    instructions: str


_PRESETS: Final[dict[StrategyArchetype, StrategyPreset]] = {
    StrategyArchetype.AGGRESSIVE: StrategyPreset(
        archetype=StrategyArchetype.AGGRESSIVE,
        public_label="Aggressive",
        instructions=(
            "Pursue positive expected-value trades actively. Prefer using "
            "available liquidity and quoting nearer the legal hard boundary "
            "when the evidence is favorable, while never violating cash, "
            "inventory, limit-price, deadline, or settlement constraints."
        ),
    ),
    StrategyArchetype.CONSERVATIVE: StrategyPreset(
        archetype=StrategyArchetype.CONSERVATIVE,
        public_label="Conservative",
        instructions=(
            "Protect marked net worth and require a clear safety margin. "
            "Preserve liquidity, avoid weakly supported exposure, and prefer "
            "pass or reject when evidence or executable margin is insufficient."
        ),
    ),
    StrategyArchetype.BALANCED: StrategyPreset(
        archetype=StrategyArchetype.BALANCED,
        public_label="Balanced",
        instructions=(
            "Balance expected return, liquidity, inventory concentration, and "
            "execution probability. Take a trade when its risk-adjusted value "
            "is positive and otherwise preserve optionality."
        ),
    ),
    StrategyArchetype.CUSTOM: StrategyPreset(
        archetype=StrategyArchetype.CUSTOM,
        public_label="Custom",
        instructions=(
            "Follow the frozen owner strategy while applying the same Arena "
            "risk, legality, privacy, and deadline constraints."
        ),
    ),
}

_OFFICIAL_ARCHETYPE_CYCLE: Final[tuple[StrategyArchetype, ...]] = (
    StrategyArchetype.CONSERVATIVE,
    StrategyArchetype.AGGRESSIVE,
    StrategyArchetype.CONSERVATIVE,
    StrategyArchetype.AGGRESSIVE,
    StrategyArchetype.BALANCED,
    StrategyArchetype.CONSERVATIVE,
    StrategyArchetype.AGGRESSIVE,
    StrategyArchetype.BALANCED,
    StrategyArchetype.BALANCED,
    StrategyArchetype.AGGRESSIVE,
)


def strategy_preset(
    archetype: StrategyArchetype | str,
) -> StrategyPreset:
    try:
        resolved = StrategyArchetype(archetype)
    except (TypeError, ValueError):
        raise ValueError("unsupported Hosted Agent strategy archetype") from None
    return _PRESETS[resolved]


def official_strategy_archetype(index: int) -> StrategyArchetype:
    if type(index) is not int or index < 1:
        raise ValueError("official Agent index must be positive")
    return _OFFICIAL_ARCHETYPE_CYCLE[
        (index - 1) % len(_OFFICIAL_ARCHETYPE_CYCLE)
    ]


def render_strategy_revision(
    *,
    archetype: StrategyArchetype | str,
    variant_instructions: str,
) -> str:
    if type(variant_instructions) is not str or not variant_instructions.strip():
        raise ValueError("variant instructions must be non-empty")
    preset = strategy_preset(archetype)
    return (
        f"Strategy catalog {STRATEGY_CATALOG_VERSION_V1}; public archetype "
        f"{preset.archetype.value}. {preset.instructions} "
        f"Private numeric variant: {variant_instructions.strip()}"
    )


__all__ = [
    "STRATEGY_CATALOG_VERSION_V1",
    "StrategyArchetype",
    "StrategyPreset",
    "official_strategy_archetype",
    "render_strategy_revision",
    "strategy_preset",
]
