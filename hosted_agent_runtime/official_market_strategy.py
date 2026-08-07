"""Versioned official-Agent market strategies for Arena games and A/Bs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping

from arena_game.goods import GOOD_IDS, GoodId

from .strategy import (
    StrategyArchetype,
    official_strategy_archetype,
    render_strategy_revision,
)


OFFICIAL_MARKET_STRATEGY_RELEASE_V2: Final = (
    "arena.official-market-strategy.liquidity-v2"
)
# Compatibility alias retained for existing experiment manifests.
EXPERIMENTAL_OFFICIAL_STRATEGY_RELEASE_V2: Final = (
    OFFICIAL_MARKET_STRATEGY_RELEASE_V2
)


@dataclass(frozen=True, slots=True)
class OfficialMarketStrategy:
    release_id: str
    archetype: StrategyArchetype
    agent_good_offsets_bps: Mapping[GoodId, int]
    instructions: str


_PROFILE_PARAMETERS: Final[
    tuple[tuple[int, int, str, str], ...]
] = (
    (900, 35, "2 units per good", "grain > iron > warhorse > gems"),
    (700, 25, "2 units in the strongest goods", "iron > gems > grain > warhorse"),
    (-700, 60, "1 unit per good", "gems > warhorse > iron > grain"),
    (-1_000, 65, "0 to 1 units per good", "warhorse > gems > iron > grain"),
    (100, 40, "1 unit per good", "grain > warhorse > iron > gems"),
    (-100, 45, "1 unit per good", "gems > grain > warhorse > iron"),
    (500, 30, "1 to 2 positive-edge units", "iron > warhorse > gems > grain"),
    (-500, 50, "1 unit per good", "grain > gems > iron > warhorse"),
    (250, 45, "1 diversified unit per good", "warhorse > iron > grain > gems"),
    (-250, 25, "1 unit early and 0 late", "gems > iron > grain > warhorse"),
)


def _agent_good_offsets(index: int) -> Mapping[GoodId, int]:
    offsets: dict[GoodId, int] = {}
    for good in GOOD_IDS:
        digest = hashlib.sha256(
            (
                f"{OFFICIAL_MARKET_STRATEGY_RELEASE_V2}\0"
                f"{index}\0{good}"
            ).encode("utf-8")
        ).digest()
        offsets[good] = int.from_bytes(digest[:2], "big") % 701 - 350
    return MappingProxyType(offsets)


def official_market_strategy_v2(index: int) -> OfficialMarketStrategy:
    """Return one deterministic, private liquidity-treatment strategy."""

    if type(index) is not int or index < 1:
        raise ValueError("official Agent index must be positive")
    (
        profile_adjustment_bps,
        cash_reserve_percent,
        inventory_target,
        good_order,
    ) = _PROFILE_PARAMETERS[(index - 1) % len(_PROFILE_PARAMETERS)]
    offsets = _agent_good_offsets(index)
    offset_text = ", ".join(
        f"{good}={offsets[good]:+d}bps" for good in GOOD_IDS
    )
    variant = (
        "You are an official Arena 402 market participant. "
        f"Strategy release {OFFICIAL_MARKET_STRATEGY_RELEASE_V2}. "
        f"Base private valuation adjustment {profile_adjustment_bps:+d}bps; "
        f"cash reserve target {cash_reserve_percent}% of marked net worth; "
        f"inventory utility center {inventory_target}; deterministic "
        f"equal-signal good order {good_order}. Frozen Agent-good private "
        f"offsets: {offset_text}. Start from the public eventImpliedFinal "
        "value and apply the base adjustment plus the frozen Agent-good "
        "offset. Then apply an inventory shadow adjustment: +600bps when "
        "holding zero units, -200bps when holding exactly the utility-center "
        "quantity, and -600bps when above it. When cash is below the reserve, "
        "apply another -400bps to held goods; when cash is safely above the "
        "reserve, apply +200bps only when evaluating a buy. In the final two "
        "rounds apply -200bps to held speculative goods. Always clamp the "
        "combined private adjustment to [-1600bps,+1600bps]. The inventory "
        "utility center is not a hard prohibition: the Agent may sell a "
        "target unit when the executable market price is at least 2% above "
        "its adjusted private fair value. It may buy when market price is at "
        "least 2% below adjusted fair value and the post-trade cash reserve "
        "remains legal. Use adjusted fair value as the target price. Use a "
        "4% worse price as the walk-away boundary; never quote beyond the "
        "task limitPrice. During negotiation, accept immediately inside the "
        "acceptable boundary, otherwise counter once toward the target while "
        "remaining inside the hard limit, and close the final turn with "
        "accept or reject. Evaluate every legal good before passing. Do not "
        "force an Intent, reveal private offsets or reasoning, reuse expired "
        "prices, or violate cash, inventory, deadline, quantity, or settlement "
        "constraints."
    )
    archetype = official_strategy_archetype(index)
    return OfficialMarketStrategy(
        release_id=OFFICIAL_MARKET_STRATEGY_RELEASE_V2,
        archetype=archetype,
        agent_good_offsets_bps=offsets,
        instructions=render_strategy_revision(
            archetype=archetype,
            variant_instructions=variant,
        ),
    )


__all__ = [
    "EXPERIMENTAL_OFFICIAL_STRATEGY_RELEASE_V2",
    "OFFICIAL_MARKET_STRATEGY_RELEASE_V2",
    "OfficialMarketStrategy",
    "official_market_strategy_v2",
]
