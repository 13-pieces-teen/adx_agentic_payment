from pathlib import Path

import pytest
from pydantic import SecretStr

from scripts.bootstrap_official_agent_pool import (
    _load_api_key,
    _owner_id,
    _strategy,
)


def test_api_key_is_loaded_as_redacted_secret(tmp_path: Path) -> None:
    key_file = tmp_path / "deepseek.key"
    key_file.write_text("test-secret-value\n", encoding="utf-8")

    value = _load_api_key(key_file)

    assert isinstance(value, SecretStr)
    assert value.get_secret_value() == "test-secret-value"
    assert "test-secret-value" not in repr(value)


def test_official_agents_have_distinct_owners_and_market_preferences() -> None:
    assert _owner_id(1) != _owner_id(2)
    assert "buying" in _strategy(1)
    assert "selling" in _strategy(2)


@pytest.mark.parametrize("contents", ["", "two words", "line1\nline2"])
def test_api_key_file_rejects_invalid_material(
    tmp_path: Path,
    contents: str,
) -> None:
    key_file = tmp_path / "deepseek.key"
    key_file.write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match="DeepSeek API key file"):
        _load_api_key(key_file)
