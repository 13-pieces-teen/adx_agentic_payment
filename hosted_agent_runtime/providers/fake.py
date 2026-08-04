"""Deterministic, network-free Provider used by Hosted Runtime tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

from hosted_agent_runtime.secret_store import WorkerSecret

from .base import (
    ProviderInvocationError,
    ProviderRequest,
    ProviderResponse,
    ProviderUsage,
)


class FakeProviderScenario(str, Enum):
    BUY = "buy"
    SELL = "sell"
    PASS = "pass"
    PROPOSE = "propose"
    PROPOSE_AT_LIMIT = "propose_at_limit"
    ACCEPT = "accept"
    REJECT = "reject"
    MARKET_BUY = "market_buy"
    MARKET_RFQ = "market_rfq"
    MARKET_ENGAGE = "market_engage"
    RATE_LIMITED = "rate_limited"
    PERMANENT_400 = "permanent_400"
    AUTHENTICATION_401 = "authentication_401"
    SERVER_5XX = "server_5xx"
    TRANSPORT_TIMEOUT = "transport_timeout"
    INVALID_JSON = "invalid_json"
    EXTRA_FIELD = "extra_field"
    MISSING_USAGE = "missing_usage"
    REASONING_TEXT = "reasoning_text"
    UNKNOWN_AFTER_REQUEST_SENT = "unknown_after_request_sent"


@dataclass(frozen=True, slots=True)
class FakeProviderStep:
    scenario: FakeProviderScenario
    delay_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, FakeProviderScenario):
            raise ValueError("fake provider scenario is invalid")
        if type(self.delay_ms) is not int or self.delay_ms < 0:
            raise ValueError("fake provider delay must be non-negative")


_COMPLETE_USAGE = ProviderUsage(
    input_tokens=21,
    output_tokens=5,
    cached_input_tokens=3,
    reasoning_tokens=2,
    complete=True,
)


class FakeProvider:
    """Run a finite deterministic script without reading the credential.

    ``on_invoke`` is a test-clock hook. It receives only the public scenario,
    never the credential or prompt.
    """

    def __init__(
        self,
        steps: Iterable[FakeProviderStep | FakeProviderScenario],
        *,
        adapter_id: str = "fake-structured",
        on_invoke: Callable[[FakeProviderScenario], None] | None = None,
    ) -> None:
        self._adapter_id = adapter_id
        self._steps = [
            step
            if isinstance(step, FakeProviderStep)
            else FakeProviderStep(step)
            for step in steps
        ]
        self._requests: list[ProviderRequest] = []
        self._on_invoke = on_invoke

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    @property
    def requests(self) -> tuple[ProviderRequest, ...]:
        return tuple(self._requests)

    async def invoke(
        self,
        request: ProviderRequest,
        credential: WorkerSecret,
    ) -> ProviderResponse:
        if not isinstance(request, ProviderRequest):
            raise ProviderInvocationError("permanent_request")
        if not isinstance(credential, WorkerSecret):
            raise ProviderInvocationError("authentication_failed")

        call_number = len(self._requests) + 1
        self._requests.append(request)
        if call_number > len(self._steps):
            raise ProviderInvocationError("script_exhausted")

        step = self._steps[call_number - 1]
        if self._on_invoke is not None:
            self._on_invoke(step.scenario)
        if step.delay_ms:
            await asyncio.sleep(step.delay_ms / 1000)

        scenario = step.scenario
        if scenario is FakeProviderScenario.RATE_LIMITED:
            raise ProviderInvocationError("rate_limited")
        if scenario is FakeProviderScenario.PERMANENT_400:
            raise ProviderInvocationError("permanent_request")
        if scenario is FakeProviderScenario.AUTHENTICATION_401:
            raise ProviderInvocationError("authentication_failed")
        if scenario is FakeProviderScenario.SERVER_5XX:
            raise ProviderInvocationError("provider_unavailable")
        if scenario is FakeProviderScenario.TRANSPORT_TIMEOUT:
            # This fake classification means the adapter proved that no
            # request bytes reached the Provider. Ambiguous/read timeouts must
            # use request_outcome_unknown instead.
            raise ProviderInvocationError("transport_failure_before_send")
        if scenario is FakeProviderScenario.INVALID_JSON:
            # A real adapter parses its raw response internally. The fake emits
            # only the sanitized parse classification, never the raw body.
            raise ProviderInvocationError("invalid_json")
        if scenario is FakeProviderScenario.UNKNOWN_AFTER_REQUEST_SENT:
            raise ProviderInvocationError("request_outcome_unknown")

        usage = _COMPLETE_USAGE
        if scenario is FakeProviderScenario.MISSING_USAGE:
            output: dict[str, object] = {"action": "pass"}
            usage = ProviderUsage.incomplete()
        elif scenario is FakeProviderScenario.REASONING_TEXT:
            # Simulate a provider payload containing private reasoning. The
            # field exists only in this parsing scope and is deliberately not
            # represented by ProviderResponse.
            provider_payload: dict[str, object] = {
                "structuredOutput": {"action": "pass"},
                "reasoningText": "private provider reasoning must be dropped",
            }
            structured = provider_payload["structuredOutput"]
            assert isinstance(structured, dict)
            output = dict(structured)
            del provider_payload
        elif scenario is FakeProviderScenario.EXTRA_FIELD:
            output = {"action": "pass", "unexpected": "rejected-by-driver"}
        else:
            output = self._action_for(scenario)

        return ProviderResponse(
            structured_output=output,
            usage=usage,
            provider_request_id=f"fake-request-{call_number}",
        )

    @staticmethod
    def _action_for(scenario: FakeProviderScenario) -> dict[str, object]:
        if scenario is FakeProviderScenario.BUY:
            return {"action": "buy", "good": "ruby"}
        if scenario is FakeProviderScenario.SELL:
            return {"action": "sell", "good": "ruby"}
        if scenario is FakeProviderScenario.PASS:
            return {"action": "pass"}
        if scenario is FakeProviderScenario.PROPOSE:
            return {
                "action": "propose",
                "price": "12.500000",
                "message": "A deterministic public proposal.",
            }
        if scenario is FakeProviderScenario.PROPOSE_AT_LIMIT:
            return {
                "action": "propose",
                "price": "10.000000",
                "message": "A corrected proposal at the hard limit.",
            }
        if scenario is FakeProviderScenario.ACCEPT:
            return {"action": "accept"}
        if scenario is FakeProviderScenario.REJECT:
            return {
                "action": "reject",
                "message": "A deterministic public rejection.",
            }
        if scenario is FakeProviderScenario.MARKET_BUY:
            return {
                "action": "buy",
                "good": "ruby",
                "publicPrice": "11.500000",
                "limitPrice": "12.000000",
                "message": "Seeking one ruby lot.",
            }
        if scenario is FakeProviderScenario.MARKET_RFQ:
            return {
                "action": "request_negotiations",
                "requests": [
                    {
                        "targetIntentId": "seller-intent-1",
                        "openingPrice": "11.000000",
                        "message": "I choose to negotiate with you.",
                    }
                ],
            }
        if scenario is FakeProviderScenario.MARKET_ENGAGE:
            return {
                "action": "engage",
                "requestId": "request-1",
            }
        raise ProviderInvocationError("permanent_request")


__all__ = [
    "FakeProvider",
    "FakeProviderScenario",
    "FakeProviderStep",
]
