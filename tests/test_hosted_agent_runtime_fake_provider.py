"""Deterministic Provider boundary tests without network or real secrets."""

from __future__ import annotations

import asyncio
from dataclasses import fields

import pytest

from hosted_agent_runtime.providers import (
    FakeProvider,
    FakeProviderScenario,
    ProviderInvocationError,
    ProviderRequest,
    ProviderResponse,
    ProviderUsage,
)
from hosted_agent_runtime.secret_store import WorkerSecret


def _request() -> ProviderRequest:
    return ProviderRequest(
        attempt_id="attempt-1",
        task_id="task-1",
        task_kind="arena.decide",
        idempotency_key="game:round:agent:decide",
        model_id="fake-model-2026-07-24",
        prompt_version="arena.hosted-prompt.v1",
        context_version="arena.agent-task.v1",
        output_version="arena.agent-action.v1",
        system_instructions="Return one structured action.",
        input_json='{"untrustedArenaData":{}}',
        output_schema_json='{"type":"object"}',
        thinking_enabled=True,
        thinking_parameter_name="thinking.enabled",
        max_output_tokens=256,
        request_timeout_ms=1_000,
    )


async def _invoke(
    provider: FakeProvider,
) -> ProviderResponse:
    with WorkerSecret(b"test-only-key") as credential:
        return await provider.invoke(_request(), credential)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        (FakeProviderScenario.BUY, {"action": "buy", "good": "ruby"}),
        (FakeProviderScenario.SELL, {"action": "sell", "good": "ruby"}),
        (FakeProviderScenario.PASS, {"action": "pass"}),
        (
            FakeProviderScenario.PROPOSE,
            {
                "action": "propose",
                "price": "12.500000",
                "message": "A deterministic public proposal.",
            },
        ),
        (FakeProviderScenario.ACCEPT, {"action": "accept"}),
        (
            FakeProviderScenario.REJECT,
            {
                "action": "reject",
                "message": "A deterministic public rejection.",
            },
        ),
    ],
)
def test_fake_provider_returns_each_structured_action(
    scenario: FakeProviderScenario,
    expected: dict[str, object],
) -> None:
    response = asyncio.run(_invoke(FakeProvider([scenario])))
    assert dict(response.structured_output) == expected
    assert response.usage.complete is True
    assert response.provider_request_id == "fake-request-1"


@pytest.mark.parametrize(
    ("scenario", "code", "retryable", "invalid", "unknown"),
    [
        (
            FakeProviderScenario.RATE_LIMITED,
            "rate_limited",
            True,
            False,
            False,
        ),
        (
            FakeProviderScenario.PERMANENT_400,
            "permanent_request",
            False,
            False,
            False,
        ),
        (
            FakeProviderScenario.AUTHENTICATION_401,
            "authentication_failed",
            False,
            False,
            False,
        ),
        (
            FakeProviderScenario.SERVER_5XX,
            "provider_unavailable",
            True,
            False,
            False,
        ),
        (
            FakeProviderScenario.TRANSPORT_TIMEOUT,
            "transport_failure_before_send",
            True,
            False,
            False,
        ),
        (
            FakeProviderScenario.INVALID_JSON,
            "invalid_json",
            False,
            True,
            False,
        ),
        (
            FakeProviderScenario.UNKNOWN_AFTER_REQUEST_SENT,
            "request_outcome_unknown",
            False,
            False,
            True,
        ),
    ],
)
def test_fake_provider_exposes_only_safe_failure_classification(
    scenario: FakeProviderScenario,
    code: str,
    retryable: bool,
    invalid: bool,
    unknown: bool,
) -> None:
    async def scenario_run() -> None:
        with pytest.raises(ProviderInvocationError) as exc:
            await _invoke(FakeProvider([scenario]))
        assert exc.value.code == code
        assert exc.value.retryable is retryable
        assert exc.value.invalid_output is invalid
        assert exc.value.outcome_unknown is unknown
        assert scenario.value not in str(exc.value) or scenario.value == code

    asyncio.run(scenario_run())


def test_usage_absence_is_explicit_and_not_inferred() -> None:
    response = asyncio.run(
        _invoke(FakeProvider([FakeProviderScenario.MISSING_USAGE]))
    )
    assert response.usage == ProviderUsage.incomplete()
    assert response.usage.complete is False
    assert response.usage.input_tokens is None
    assert response.usage.output_tokens is None
    assert response.usage.cached_input_tokens is None
    assert response.usage.reasoning_tokens is None


def test_reasoning_payload_is_discarded_at_provider_parse_boundary() -> None:
    response = asyncio.run(
        _invoke(FakeProvider([FakeProviderScenario.REASONING_TEXT]))
    )
    assert dict(response.structured_output) == {"action": "pass"}
    response_fields = {field.name.casefold() for field in fields(response)}
    assert response_fields == {
        "structured_output",
        "usage",
        "provider_request_id",
        "actual_model",
    }
    assert not any(
        marker in name
        for name in response_fields
        for marker in ("reasoning", "thought", "raw", "body")
    )


def test_request_and_response_types_cannot_carry_raw_secrets_or_bodies() -> None:
    forbidden = ("secret", "credential", "api_key", "reasoning", "raw", "body")
    for model in (ProviderRequest, ProviderResponse):
        names = {field.name.casefold() for field in fields(model)}
        assert not any(
            marker in name
            for name in names
            for marker in forbidden
        )
    rendered = repr(_request())
    assert "untrustedArenaData" not in rendered
    assert "Return one structured action" not in rendered


def test_extra_field_stays_structured_for_driver_to_reject() -> None:
    response = asyncio.run(
        _invoke(FakeProvider([FakeProviderScenario.EXTRA_FIELD]))
    )
    assert dict(response.structured_output) == {
        "action": "pass",
        "unexpected": "rejected-by-driver",
    }


def test_provider_response_rejects_opaque_or_float_payload_values() -> None:
    with pytest.raises(ValueError, match="JSON-safe"):
        ProviderResponse(
            structured_output={"action": "pass", "raw": b"opaque"},
            usage=ProviderUsage.incomplete(),
        )
    with pytest.raises(ValueError, match="JSON-safe"):
        ProviderResponse(
            structured_output={"action": "pass", "score": 0.5},
            usage=ProviderUsage.incomplete(),
        )


def test_provider_request_id_reuses_ingress_secret_screening() -> None:
    unsafe_id = "sk-abcdefghijklmnop"
    with pytest.raises(ValueError) as exc:
        ProviderResponse(
            structured_output={"action": "pass"},
            usage=ProviderUsage.incomplete(),
            provider_request_id=unsafe_id,
        )
    assert unsafe_id not in str(exc.value)


def test_fake_script_is_finite_and_request_capture_is_secret_free() -> None:
    async def scenario_run() -> None:
        provider = FakeProvider([FakeProviderScenario.PASS])
        await _invoke(provider)
        with pytest.raises(ProviderInvocationError) as exc:
            await _invoke(provider)
        assert exc.value.code == "script_exhausted"
        assert len(provider.requests) == 2
        assert provider.requests[0] == _request()

    asyncio.run(scenario_run())
