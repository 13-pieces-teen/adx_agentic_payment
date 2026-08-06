"""Deadline and usage-bounded PydanticAI post-game learner."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from pydantic_ai import UsageLimits
from pydantic_ai.exceptions import (
    AgentRunError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from hosted_agent_runtime.providers import ProviderErrorCode, ProviderUsage

from .agent import build_strategy_learning_agent
from .models import HostedLearningEvidence, StrategyLearningProposal


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HostedLearningExecution:
    status: str
    proposal: StrategyLearningProposal | None
    usage: ProviderUsage
    request_count: int
    tool_call_count: int
    latency_ms: int
    actual_model: str | None
    error_code: ProviderErrorCode | None


@dataclass(frozen=True, slots=True)
class HostedLearningRuntimeLimits:
    request_limit: int = 4
    tool_calls_limit: int = 4
    output_tokens_limit: int = 8_192

    def __post_init__(self) -> None:
        if not 3 <= self.request_limit <= 8:
            raise ValueError("learning request_limit must be between 3 and 8")
        if not 2 <= self.tool_calls_limit <= 8:
            raise ValueError(
                "learning tool_calls_limit must be between 2 and 8"
            )
        if not 2_048 <= self.output_tokens_limit <= 16_384:
            raise ValueError(
                "learning output_tokens_limit must be between 2048 and 16384"
            )


class HostedStrategyLearningRuntime:
    def __init__(
        self,
        *,
        model: Model,
        model_settings: ModelSettings | None = None,
        actual_model: str | None = None,
        limits: HostedLearningRuntimeLimits | None = None,
    ) -> None:
        self._agent = build_strategy_learning_agent(model)
        self._model_settings = model_settings
        self._actual_model = actual_model
        self._limits = limits or HostedLearningRuntimeLimits()

    async def execute(
        self,
        evidence: HostedLearningEvidence,
        *,
        timeout_seconds: float,
    ) -> HostedLearningExecution:
        if timeout_seconds <= 0:
            raise ValueError("learning timeout must be positive")
        started = time.monotonic()
        verified_evidence = {
            "gameId": evidence.game_id,
            "archetype": evidence.archetype.value,
            "basePolicyProfile": evidence.base_policy_profile.model_dump(
                mode="json",
                by_alias=True,
            ),
            "outcome": evidence.outcome.model_dump(
                mode="json",
                by_alias=True,
            ),
            "behavior": evidence.behavior.model_dump(
                mode="json",
                by_alias=True,
            ),
            "finalPricesAtomic": dict(evidence.final_prices_atomic),
            "lastGameMemory": dict(evidence.last_game_memory),
        }
        prompt = (
            "Study the completed Arena game and propose one bounded policy "
            "update. The JSON below is the complete authoritative evidence "
            "snapshot. Do not require an additional tool call before "
            "returning the typed proposal. The read-only tools remain "
            "available if you need to re-inspect either evidence section.\n"
            f"Learning job: {evidence.learning_job_id}\n"
            "Verified evidence JSON:\n"
            + json.dumps(
                verified_evidence,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                run = await self._agent.run(
                    prompt,
                    deps=evidence,
                    model_settings=self._model_settings,
                    usage_limits=UsageLimits(
                        request_limit=self._limits.request_limit,
                        tool_calls_limit=self._limits.tool_calls_limit,
                        output_tokens_limit=self._limits.output_tokens_limit,
                    ),
                )
        except TimeoutError:
            return self._failed(
                started,
                error_code="request_outcome_unknown",
            )
        except ModelHTTPError as exc:
            return self._failed(
                started,
                error_code=self._http_error_code(exc.status_code),
            )
        except UsageLimitExceeded:
            return self._failed(started, error_code="permanent_request")
        except UnexpectedModelBehavior as exc:
            _LOGGER.warning(
                "hosted_learning_invalid_structured_output_%s",
                _unexpected_model_behavior_code(exc.message),
            )
            return self._failed(
                started,
                error_code="invalid_structured_output",
            )
        except AgentRunError:
            return self._failed(
                started,
                error_code="request_outcome_unknown",
            )

        usage = run.usage
        return HostedLearningExecution(
            status="succeeded",
            proposal=run.output,
            usage=ProviderUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_input_tokens=usage.cache_read_tokens,
                reasoning_tokens=int(
                    usage.details.get("reasoning_tokens", 0)
                ),
                complete=True,
            ),
            request_count=usage.requests,
            tool_call_count=usage.tool_calls,
            latency_ms=self._latency_ms(started),
            actual_model=self._actual_model,
            error_code=None,
        )

    def _failed(
        self,
        started: float,
        *,
        error_code: ProviderErrorCode,
    ) -> HostedLearningExecution:
        return HostedLearningExecution(
            status="failed",
            proposal=None,
            usage=ProviderUsage.incomplete(),
            request_count=0,
            tool_call_count=0,
            latency_ms=self._latency_ms(started),
            actual_model=self._actual_model,
            error_code=error_code,
        )

    @staticmethod
    def _http_error_code(status_code: int) -> ProviderErrorCode:
        if status_code in {401, 403}:
            return "authentication_failed"
        if status_code == 429:
            return "rate_limited"
        if status_code >= 500:
            return "provider_unavailable"
        return "permanent_request"

    @staticmethod
    def _latency_ms(started: float) -> int:
        return max(0, int((time.monotonic() - started) * 1000))


def _unexpected_model_behavior_code(message: str) -> str:
    normalized = message.strip().lower()
    if normalized.startswith("model token limit"):
        return "token_limit"
    if normalized.startswith("exceeded maximum output retries"):
        return "output_retry_exhausted"
    if normalized.startswith("tool ") and "max retries" in normalized:
        return "tool_retry_exhausted"
    if normalized.startswith("invalid response"):
        return "invalid_response"
    return "unexpected_model_behavior"


__all__ = [
    "HostedLearningExecution",
    "HostedLearningRuntimeLimits",
    "HostedStrategyLearningRuntime",
]
