"""Provider/model capability allowlist tests for Hosted Arena Agents."""

from __future__ import annotations

from dataclasses import fields

import pytest

from hosted_agent_runtime.capabilities import (
    CapabilityError,
    CapabilityRegistry,
    ModelCapability,
    ResolvedModelCapability,
    ThinkingEffortPolicy,
    ThinkingMode,
    assert_no_private_reasoning_fields,
)


def _capability(
    *,
    provider_id: str = "provider-a",
    model_id: str = "model-2026-07-01",
    thinking_mode: ThinkingMode = ThinkingMode.OPTIONAL,
    thinking_parameter_name: str | None = "thinking.enabled",
    verified: bool = True,
    enabled: bool = True,
    supports_structured_output: bool = True,
) -> ModelCapability:
    return ModelCapability(
        provider_id=provider_id,
        adapter_id=f"{provider_id}-responses",
        model_id=model_id,
        display_name=f"{provider_id} {model_id}",
        supports_structured_output=supports_structured_output,
        thinking_mode=thinking_mode,
        thinking_parameter_name=thinking_parameter_name,
        max_output_tokens=8192,
        request_timeout_cap_ms=90_000,
        adapter_version="adapter-v1",
        immutable_model_id=True,
        verified=verified,
        enabled=enabled,
    )


def test_public_registry_exposes_only_safe_verified_metadata() -> None:
    enabled = _capability()
    disabled = _capability(
        provider_id="provider-b",
        model_id="model-2026-06-01",
        verified=False,
        enabled=False,
    )
    registry = CapabilityRegistry([disabled, enabled])

    public = registry.list_public()
    assert len(public) == 1
    payload = public[0].to_dict()
    assert payload["providerId"] == enabled.provider_id
    assert payload["modelId"] == enabled.model_id
    assert payload["thinkingCanToggle"] is True
    assert payload["effectiveThinkingDefault"] is False

    serialized_keys = {key.lower() for key in payload}
    assert "adapterid" not in serialized_keys
    assert "adapterversion" not in serialized_keys
    assert "thinkingparametername" not in serialized_keys
    assert not any(
        forbidden in key
        for key in serialized_keys
        for forbidden in ("chainofthought", "reasoningtext", "reasoningtrace")
    )


@pytest.mark.parametrize("thinking_enabled", [False, True])
def test_optional_thinking_uses_boolean_and_provider_default_effort(
    thinking_enabled: bool,
) -> None:
    registry = CapabilityRegistry([_capability()])
    resolved = registry.resolve(
        provider_id="provider-a",
        model_id="model-2026-07-01",
        thinking_enabled=thinking_enabled,
        remaining_timeout_ms=120_000,
        requested_max_output_tokens=2048,
    )

    assert resolved.thinking_enabled is thinking_enabled
    assert resolved.thinking_parameter_name == "thinking.enabled"
    assert (
        resolved.thinking_effort_policy
        is ThinkingEffortPolicy.PROVIDER_DEFAULT
    )
    assert resolved.request_timeout_ms == 90_000
    assert resolved.max_output_tokens == 2048


def test_default_output_limit_and_shorter_remaining_timeout_win() -> None:
    registry = CapabilityRegistry([_capability()])
    resolved = registry.resolve(
        provider_id="provider-a",
        model_id="model-2026-07-01",
        thinking_enabled=False,
        remaining_timeout_ms=15_000,
    )
    assert resolved.max_output_tokens == 8192
    assert resolved.request_timeout_ms == 15_000


def test_unsupported_thinking_cannot_be_enabled() -> None:
    capability = _capability(
        thinking_mode=ThinkingMode.UNSUPPORTED,
        thinking_parameter_name=None,
    )
    registry = CapabilityRegistry([capability])

    with pytest.raises(CapabilityError) as exc:
        registry.resolve(
            provider_id=capability.provider_id,
            model_id=capability.model_id,
            thinking_enabled=True,
            remaining_timeout_ms=10_000,
        )
    assert exc.value.code == "thinking_not_supported"

    resolved = registry.resolve(
        provider_id=capability.provider_id,
        model_id=capability.model_id,
        thinking_enabled=False,
        remaining_timeout_ms=10_000,
    )
    assert resolved.thinking_enabled is False
    assert resolved.thinking_parameter_name is None


def test_always_on_thinking_cannot_be_disabled() -> None:
    capability = _capability(
        thinking_mode=ThinkingMode.ALWAYS_ON,
        thinking_parameter_name=None,
    )
    registry = CapabilityRegistry([capability])

    with pytest.raises(CapabilityError) as exc:
        registry.resolve(
            provider_id=capability.provider_id,
            model_id=capability.model_id,
            thinking_enabled=False,
            remaining_timeout_ms=10_000,
        )
    assert exc.value.code == "thinking_always_on"

    resolved = registry.resolve(
        provider_id=capability.provider_id,
        model_id=capability.model_id,
        thinking_enabled=True,
        remaining_timeout_ms=10_000,
    )
    assert resolved.thinking_enabled is True
    assert (
        resolved.thinking_effort_policy
        is ThinkingEffortPolicy.PROVIDER_DEFAULT
    )


@pytest.mark.parametrize("requested_tokens", [0, -1, 8193, True])
def test_invalid_output_token_request_is_rejected(
    requested_tokens: int,
) -> None:
    registry = CapabilityRegistry([_capability()])
    with pytest.raises(CapabilityError) as exc:
        registry.resolve(
            provider_id="provider-a",
            model_id="model-2026-07-01",
            thinking_enabled=False,
            remaining_timeout_ms=10_000,
            requested_max_output_tokens=requested_tokens,
        )
    assert exc.value.code == "output_token_limit_exceeded"


@pytest.mark.parametrize(
    "capability",
    [
        pytest.param(
            lambda: _capability(verified=False, enabled=True),
            id="enabled-unverified",
        ),
        pytest.param(
            lambda: _capability(
                supports_structured_output=False,
                enabled=True,
            ),
            id="enabled-without-structured-output",
        ),
        pytest.param(
            lambda: _capability(model_id="model-latest"),
            id="mutable-model-alias",
        ),
        pytest.param(
            lambda: _capability(
                thinking_parameter_name="reasoning_trace",
            ),
            id="private-reasoning-parameter",
        ),
    ],
)
def test_unsafe_capability_definitions_are_rejected(
    capability: object,
) -> None:
    with pytest.raises(CapabilityError):
        capability()  # type: ignore[operator]


def test_registry_is_unique_and_unknown_models_fail_closed() -> None:
    capability = _capability()
    with pytest.raises(CapabilityError) as exc:
        CapabilityRegistry([capability, capability])
    assert exc.value.code == "duplicate_capability"

    with pytest.raises(CapabilityError) as exc:
        CapabilityRegistry().resolve(
            provider_id="provider-a",
            model_id="model-2026-07-01",
            thinking_enabled=False,
            remaining_timeout_ms=10_000,
        )
    assert exc.value.code == "model_not_available"


def test_runtime_models_have_no_private_reasoning_payload_fields() -> None:
    assert_no_private_reasoning_fields()
    names = {field.name for field in fields(ResolvedModelCapability)}
    assert names.isdisjoint(
        {
            "chain_of_thought",
            "reasoning_text",
            "reasoning_content",
            "reasoning_trace",
            "thinking_text",
            "thinking_content",
            "thinking_trace",
        }
    )
