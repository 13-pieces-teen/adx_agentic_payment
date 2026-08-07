from pathlib import Path

import pytest
from pydantic import SecretStr

from hosted_agent_runtime.runtime_contract import MAX_STRATEGY_BYTES
from hosted_agent_runtime.strategy import official_strategy_archetype
from scripts.bootstrap_official_agent_pool import (
    _load_litellm_token,
    _owner_id,
    _require_healthy_litellm_payload,
    _strategy,
)
from scripts.refresh_official_agent_strategies import (
    STRATEGY_VERSION,
    _parser as _refresh_parser,
    _select_official_rows,
    _strategy as _refresh_strategy,
    _update_idempotency_key,
)


def test_litellm_token_is_loaded_as_redacted_secret(tmp_path: Path) -> None:
    key_file = tmp_path / "litellm.key"
    key_file.write_text("sk-test-secret-value\n", encoding="utf-8")

    value = _load_litellm_token(key_file)

    assert isinstance(value, SecretStr)
    assert value.get_secret_value() == "sk-test-secret-value"
    assert "sk-test-secret-value" not in repr(value)


def test_official_agents_cycle_through_ten_numeric_market_profiles() -> None:
    assert _owner_id(1) != _owner_id(2)
    first_cycle = [_strategy(index) for index in range(1, 11)]
    second_cycle = [_strategy(index) for index in range(11, 21)]

    assert len(set(first_cycle)) == 10
    assert len(set(first_cycle + second_cycle)) == 20
    assert all("adjusted private fair value" in strategy for strategy in first_cycle)
    assert all("eventImpliedFinal" in strategy for strategy in first_cycle)
    assert all("private adjustment" in strategy for strategy in first_cycle)
    assert all("public archetype" in strategy for strategy in first_cycle)
    assert len(
        {official_strategy_archetype(index) for index in range(1, 11)}
    ) == 3
    assert all("cash reserve" in strategy for strategy in first_cycle)
    assert all("inventory utility center" in strategy for strategy in first_cycle)
    assert all(
        (
            "Strategy release "
            "arena.official-market-strategy.liquidity-v2"
        )
        in strategy
        for strategy in first_cycle
    )
    assert all(
        "inventory utility center is not a hard prohibition"
        in strategy
        for strategy in first_cycle
    )
    assert all(
        "Evaluate every legal good before passing"
        in strategy
        for strategy in first_cycle
    )
    assert all(
        len(strategy.encode("utf-8")) <= MAX_STRATEGY_BYTES
        for strategy in first_cycle
    )
    assert [
        _refresh_strategy(index) for index in range(1, 21)
    ] == first_cycle + second_cycle


def test_official_strategy_refresh_has_a_versioned_stable_idempotency_key() -> None:
    assert (
        STRATEGY_VERSION
        == "arena.official-market-strategy.liquidity-v2"
    )
    assert (
        _update_idempotency_key("agent-official-001")
        == (
            "official-strategy-"
            "arena.official-market-strategy.liquidity-v2-"
            "agent-official-001"
        )
    )


def test_official_strategy_refresh_can_select_specific_priorities() -> None:
    rows = [
        {"agent_id": "agent-1", "priority": 1},
        {"agent_id": "agent-3", "priority": 3},
        {"agent_id": "agent-4", "priority": 4},
    ]

    selected = _select_official_rows(rows, priorities=(3, 4))

    assert [row["agent_id"] for row in selected] == [
        "agent-3",
        "agent-4",
    ]

    with pytest.raises(RuntimeError, match="not enabled"):
        _select_official_rows(rows, priorities=(2,))

    with pytest.raises(RuntimeError, match="unique"):
        _select_official_rows(rows, priorities=(3, 3))


def test_official_strategy_refresh_cli_accepts_repeatable_priority() -> None:
    args = _refresh_parser().parse_args(
        ["--priority", "3", "--priority", "4"]
    )

    assert args.priority == [3, 4]


@pytest.mark.parametrize("contents", ["", "two words", "line1\nline2"])
def test_litellm_token_file_rejects_invalid_material(
    tmp_path: Path,
    contents: str,
) -> None:
    key_file = tmp_path / "litellm.key"
    key_file.write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match="LiteLLM token"):
        _load_litellm_token(key_file)


def test_official_pool_requires_every_litellm_deployment_to_be_healthy() -> None:
    assert (
        _require_healthy_litellm_payload(
            {
                "healthy_endpoints": [
                    {"model": "deepseek/deepseek-v4-flash"}
                ],
                "unhealthy_endpoints": [],
            }
        )
        == 1
    )

    with pytest.raises(RuntimeError, match="unhealthy"):
        _require_healthy_litellm_payload(
            {
                "healthy_endpoints": [
                    {"model": "deepseek/deepseek-v4-flash"}
                ],
                "unhealthy_endpoints": [
                    {"model": "deepseek/deepseek-v4-flash"}
                ],
            }
        )
