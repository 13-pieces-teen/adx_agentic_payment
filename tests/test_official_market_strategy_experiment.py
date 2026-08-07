from __future__ import annotations

from hosted_agent_runtime.official_market_strategy import (
    EXPERIMENTAL_OFFICIAL_STRATEGY_RELEASE_V2,
    official_market_strategy_v2,
)


def test_liquidity_v2_strategy_is_stable_and_softens_inventory_targets() -> None:
    first = official_market_strategy_v2(5)
    replay = official_market_strategy_v2(5)
    other = official_market_strategy_v2(6)

    assert first == replay
    assert first.release_id == EXPERIMENTAL_OFFICIAL_STRATEGY_RELEASE_V2
    assert set(first.agent_good_offsets_bps) == {
        "grain",
        "iron",
        "warhorse",
        "gems",
    }
    assert all(
        -350 <= value <= 350
        for value in first.agent_good_offsets_bps.values()
    )
    assert (
        first.agent_good_offsets_bps
        != other.agent_good_offsets_bps
    )
    assert "not a hard prohibition" in first.instructions
    assert "sell a target unit" in first.instructions
    assert "clamp the combined private adjustment" in first.instructions
