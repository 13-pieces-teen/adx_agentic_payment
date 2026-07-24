"""Deployment-owned Provider endpoint and model allowlist."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .capabilities import (
    CapabilityRegistry,
    ModelCapability,
    ThinkingMode,
)
from .providers import (
    ARENA_SCRIPTED_ADAPTER_ID,
    ARENA_SCRIPTED_BUYER_MODEL,
    ARENA_SCRIPTED_PROVIDER_ID,
    ARENA_SCRIPTED_SELLER_MODEL,
    ArenaScriptedProvider,
    OpenAICompatibleChatAdapter,
    OpenAICompatibleSettings,
    ProviderAdapter,
)


@dataclass(frozen=True, slots=True)
class ProductionProviderBundle:
    registry: CapabilityRegistry
    adapters: dict[str, ProviderAdapter]

    async def close(self) -> None:
        for adapter in self.adapters.values():
            close = getattr(adapter, "close", None)
            if close is not None:
                await close()


def _deepseek_capability(model_id: str, display_name: str) -> ModelCapability:
    return ModelCapability(
        provider_id="deepseek",
        adapter_id="deepseek-openai-chat-v1",
        model_id=model_id,
        display_name=display_name,
        supports_structured_output=True,
        thinking_mode=ThinkingMode.OPTIONAL,
        thinking_parameter_name="thinking.enabled",
        max_output_tokens=16_384,
        request_timeout_cap_ms=300_000,
        adapter_version="deepseek-openai-chat-v1",
        immutable_model_id=True,
        verified=True,
        enabled=True,
    )


def build_production_capability_registry() -> CapabilityRegistry:
    capabilities = [
        _deepseek_capability(
            "deepseek-v4-flash",
            "DeepSeek V4 Flash",
        ),
        _deepseek_capability(
            "deepseek-v4-pro",
            "DeepSeek V4 Pro",
        ),
    ]
    # Additional OpenAI-compatible providers are deployment configuration,
    # not request fields. This preserves the same BYOK UX without allowing
    # an API key to be exfiltrated through an attacker-controlled base URL.
    compatible_endpoint = os.getenv(
        "ADX_OPENAI_COMPATIBLE_ENDPOINT", ""
    ).strip()
    compatible_models = tuple(
        item.strip()
        for item in os.getenv(
            "ADX_OPENAI_COMPATIBLE_MODELS", ""
        ).split(",")
        if item.strip()
    )
    if compatible_endpoint and compatible_models:
        adapter_id = "deployment-openai-compatible-chat-v1"
        # Validate the endpoint even when this process needs only the public
        # capability registry.
        OpenAICompatibleSettings(
            adapter_id=adapter_id,
            endpoint=compatible_endpoint,
            response_format_mode="json_object",
            thinking_dialect="none",
        )
        capabilities.extend(
            ModelCapability(
                provider_id="openai-compatible",
                adapter_id=adapter_id,
                model_id=model_id,
                display_name=model_id,
                supports_structured_output=True,
                thinking_mode=ThinkingMode.UNSUPPORTED,
                thinking_parameter_name=None,
                max_output_tokens=16_384,
                request_timeout_cap_ms=300_000,
                adapter_version=adapter_id,
                immutable_model_id=True,
                verified=True,
                enabled=True,
            )
            for model_id in compatible_models
        )

    return CapabilityRegistry(
        capabilities,
        registry_version="arena.provider-registry.production.v1",
    )


def build_production_provider_bundle() -> ProductionProviderBundle:
    """Build trusted endpoints; users cannot provide or override base URLs."""

    registry = build_production_capability_registry()
    adapters: dict[str, ProviderAdapter] = {
        "deepseek": OpenAICompatibleChatAdapter(
            OpenAICompatibleSettings(
                adapter_id="deepseek-openai-chat-v1",
                endpoint="https://api.deepseek.com/chat/completions",
                response_format_mode="json_object",
                thinking_dialect="deepseek",
            )
        )
    }
    if any(
        item.provider_id == "openai-compatible"
        for item in registry.list_public()
    ):
        adapters["openai-compatible"] = OpenAICompatibleChatAdapter(
            OpenAICompatibleSettings(
                adapter_id="deployment-openai-compatible-chat-v1",
                endpoint=os.environ["ADX_OPENAI_COMPATIBLE_ENDPOINT"],
                response_format_mode="json_object",
                thinking_dialect="none",
            )
        )
    return ProductionProviderBundle(registry=registry, adapters=adapters)


def build_local_development_provider_bundle() -> ProductionProviderBundle:
    """Add a network-free Provider only to the explicit development stack."""

    production = build_production_provider_bundle()
    production_capabilities = [
        _deepseek_capability(
            "deepseek-v4-flash",
            "DeepSeek V4 Flash",
        ),
        _deepseek_capability(
            "deepseek-v4-pro",
            "DeepSeek V4 Pro",
        ),
    ]
    compatible_endpoint = os.getenv(
        "ADX_OPENAI_COMPATIBLE_ENDPOINT", ""
    ).strip()
    compatible_models = tuple(
        item.strip()
        for item in os.getenv(
            "ADX_OPENAI_COMPATIBLE_MODELS", ""
        ).split(",")
        if item.strip()
    )
    if compatible_endpoint and compatible_models:
        production_capabilities.extend(
            ModelCapability(
                provider_id="openai-compatible",
                adapter_id="deployment-openai-compatible-chat-v1",
                model_id=model_id,
                display_name=model_id,
                supports_structured_output=True,
                thinking_mode=ThinkingMode.UNSUPPORTED,
                thinking_parameter_name=None,
                max_output_tokens=16_384,
                request_timeout_cap_ms=300_000,
                adapter_version="deployment-openai-compatible-chat-v1",
                immutable_model_id=True,
                verified=True,
                enabled=True,
            )
            for model_id in compatible_models
        )
    scripted = [
        ModelCapability(
            provider_id=ARENA_SCRIPTED_PROVIDER_ID,
            adapter_id=ARENA_SCRIPTED_ADAPTER_ID,
            model_id=model_id,
            display_name=display_name,
            supports_structured_output=True,
            thinking_mode=ThinkingMode.UNSUPPORTED,
            thinking_parameter_name=None,
            max_output_tokens=256,
            request_timeout_cap_ms=10_000,
            adapter_version=ARENA_SCRIPTED_ADAPTER_ID,
            immutable_model_id=True,
            verified=True,
            enabled=True,
        )
        for model_id, display_name in (
            (ARENA_SCRIPTED_BUYER_MODEL, "Arena Demo Buyer"),
            (ARENA_SCRIPTED_SELLER_MODEL, "Arena Demo Seller"),
        )
    ]
    return ProductionProviderBundle(
        registry=CapabilityRegistry(
            [*production_capabilities, *scripted],
            registry_version="arena.provider-registry.local-development.v1",
        ),
        adapters={
            **production.adapters,
            ARENA_SCRIPTED_PROVIDER_ID: ArenaScriptedProvider(),
        },
    )


__all__ = [
    "ProductionProviderBundle",
    "build_local_development_provider_bundle",
    "build_production_capability_registry",
    "build_production_provider_bundle",
]
