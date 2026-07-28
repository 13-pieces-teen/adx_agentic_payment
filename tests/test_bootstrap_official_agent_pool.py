from pathlib import Path

import pytest
from pydantic import SecretStr

from hosted_agent_runtime.prompt_builder import MAX_STRATEGY_BYTES
from scripts.bootstrap_official_agent_pool import (
    _api_key_slot,
    _load_api_key,
    _owner_id,
    _strategy,
)
from scripts.refresh_official_agent_strategies import (
    STRATEGY_VERSION,
    _update_idempotency_key,
)


def test_api_key_is_loaded_as_redacted_secret(tmp_path: Path) -> None:
    key_file = tmp_path / "deepseek.key"
    key_file.write_text("test-secret-value\n", encoding="utf-8")

    value = _load_api_key(key_file)

    assert isinstance(value, SecretStr)
    assert value.get_secret_value() == "test-secret-value"
    assert "test-secret-value" not in repr(value)


def test_official_agents_cycle_through_ten_numeric_market_profiles() -> None:
    assert _owner_id(1) != _owner_id(2)
    first_cycle = [_strategy(index) for index in range(1, 11)]
    second_cycle = [_strategy(index) for index in range(11, 21)]

    assert len(set(first_cycle)) == 10
    assert second_cycle == first_cycle
    assert all("fairValue" in strategy for strategy in first_cycle)
    assert all("cash reserve" in strategy for strategy in first_cycle)
    assert all("inventory target" in strategy for strategy in first_cycle)
    assert any("BUY_BIASED" in strategy for strategy in first_cycle)
    assert any("SELL_BIASED" in strategy for strategy in first_cycle)
    assert any("TWO_SIDED" in strategy for strategy in first_cycle)
    assert all("expired event price" in strategy for strategy in first_cycle)
    assert all(
        "build the legal candidate set" in strategy for strategy in first_cycle
    )
    assert all("zero-holding good" in strategy for strategy in first_cycle)
    assert all("next legal good" in strategy for strategy in first_cycle)
    assert all(
        len(strategy.encode("utf-8")) <= MAX_STRATEGY_BYTES
        for strategy in first_cycle
    )


def test_official_strategy_refresh_has_a_versioned_stable_idempotency_key() -> None:
    assert STRATEGY_VERSION == "market-v5"
    assert (
        _update_idempotency_key("agent-official-001")
        == "official-strategy-market-v5-agent-official-001"
    )


def test_three_keys_are_distributed_as_contiguous_7_7_6_ranges() -> None:
    slots = [
        _api_key_slot(index=index, count=20, key_count=3)
        for index in range(1, 21)
    ]

    assert slots == ([0] * 7) + ([1] * 7) + ([2] * 6)


@pytest.mark.parametrize("contents", ["", "two words", "line1\nline2"])
def test_api_key_file_rejects_invalid_material(
    tmp_path: Path,
    contents: str,
) -> None:
    key_file = tmp_path / "deepseek.key"
    key_file.write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match="DeepSeek API key file"):
        _load_api_key(key_file)
