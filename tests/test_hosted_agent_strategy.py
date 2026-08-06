from collections import Counter

import pytest

from hosted_agent_runtime.strategy import (
    STRATEGY_CATALOG_VERSION_V1,
    StrategyArchetype,
    official_strategy_archetype,
    render_strategy_revision,
    strategy_preset,
)


def test_official_strategy_catalog_has_three_comparable_archetypes() -> None:
    first_cycle = [
        official_strategy_archetype(index) for index in range(1, 11)
    ]

    assert set(first_cycle) == {
        StrategyArchetype.AGGRESSIVE,
        StrategyArchetype.CONSERVATIVE,
        StrategyArchetype.BALANCED,
    }
    assert Counter(first_cycle) == {
        StrategyArchetype.AGGRESSIVE: 4,
        StrategyArchetype.CONSERVATIVE: 3,
        StrategyArchetype.BALANCED: 3,
    }
    assert all(
        set(first_cycle[:omitted] + first_cycle[omitted + 1 :])
        == set(first_cycle)
        for omitted in range(len(first_cycle))
    )
    assert [
        official_strategy_archetype(index) for index in range(11, 21)
    ] == first_cycle


def test_strategy_revision_combines_public_type_and_private_variant() -> None:
    value = render_strategy_revision(
        archetype=StrategyArchetype.AGGRESSIVE,
        variant_instructions="Prefer iron when two candidates tie.",
    )

    assert STRATEGY_CATALOG_VERSION_V1 in value
    assert "public archetype aggressive" in value
    assert "Prefer iron" in value
    assert strategy_preset("aggressive").public_label == "Aggressive"


@pytest.mark.parametrize("index", [0, -1])
def test_official_strategy_index_must_be_positive(index: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        official_strategy_archetype(index)
