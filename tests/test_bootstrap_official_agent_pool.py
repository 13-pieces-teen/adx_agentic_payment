from pathlib import Path

import pytest
from pydantic import SecretStr

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


def test_official_agents_have_distinct_owners_and_five_market_roles() -> None:
    assert _owner_id(1) != _owner_id(2)
    strategies = {_strategy(index) for index in range(1, 6)}

    assert len(strategies) == 5
    assert any("value buyer" in strategy for strategy in strategies)
    assert any("inventory seller" in strategy for strategy in strategies)
    assert any("market maker" in strategy for strategy in strategies)
    assert any("event trader" in strategy for strategy in strategies)
    assert any("portfolio allocator" in strategy for strategy in strategies)
    assert all("expired event price" in strategy for strategy in strategies)


def test_official_strategy_refresh_has_a_versioned_stable_idempotency_key() -> None:
    assert STRATEGY_VERSION == "market-v2"
    assert (
        _update_idempotency_key("agent-official-001")
        == "official-strategy-market-v2-agent-official-001"
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
