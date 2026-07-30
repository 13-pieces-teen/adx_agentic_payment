"""Official-only production Provider routing tests."""

from __future__ import annotations

import asyncio

import pytest

from hosted_agent_runtime.capabilities import CapabilityError
from hosted_agent_runtime.production_providers import (
    build_production_capability_registry,
    build_production_provider_bundle,
)
from hosted_agent_runtime.providers import OpenAICompatibleChatAdapter


def test_default_production_registry_hides_official_deepseek() -> None:
    registry = build_production_capability_registry()

    assert all(
        capability.provider_id != "official-deepseek"
        for capability in registry.list_public()
    )
    with pytest.raises(CapabilityError) as exc:
        registry.resolve(
            provider_id="official-deepseek",
            model_id="deepseek-v4-flash",
            thinking_enabled=False,
            remaining_timeout_ms=10_000,
        )
    assert exc.value.code == "model_not_available"


def test_internal_production_registry_can_resolve_official_deepseek() -> None:
    registry = build_production_capability_registry(include_official=True)

    resolved = registry.resolve(
        provider_id="official-deepseek",
        model_id="deepseek-v4-flash",
        thinking_enabled=False,
        remaining_timeout_ms=10_000,
    )

    assert resolved.provider_id == "official-deepseek"
    assert resolved.model_id == "deepseek-v4-flash"
    assert resolved.adapter_id == "official-deepseek-litellm-chat-v1"


def test_production_bundle_keeps_byok_and_official_endpoints_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADX_OPENAI_COMPATIBLE_ENDPOINT", raising=False)
    monkeypatch.delenv("ADX_OPENAI_COMPATIBLE_MODELS", raising=False)
    bundle = build_production_provider_bundle()
    try:
        deepseek = bundle.adapters["deepseek"]
        official = bundle.adapters["official-deepseek"]
        assert isinstance(deepseek, OpenAICompatibleChatAdapter)
        assert isinstance(official, OpenAICompatibleChatAdapter)

        assert deepseek._settings.endpoint == (  # noqa: SLF001
            "https://api.deepseek.com/chat/completions"
        )
        assert deepseek._settings.allow_http_hostname is None  # noqa: SLF001
        assert official._settings.endpoint == (  # noqa: SLF001
            "http://official-litellm:4000/v1/chat/completions"
        )
        assert official._settings.allow_http_hostname == (  # noqa: SLF001
            "official-litellm"
        )
    finally:
        asyncio.run(bundle.close())
