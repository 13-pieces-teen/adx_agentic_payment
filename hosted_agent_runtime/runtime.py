"""Deadline-bounded PydanticAI implementation of the Arena Runtime port."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic_ai import UsageLimits
from pydantic_ai.exceptions import (
    AgentRunError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage

from arena_agent_contracts import (
    AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
    AgentDrivenMarketActionV1,
    AgentTaskResultV1,
    ArenaAgentTaskV1,
)
from arena_core.hashing import sha256_identifier

from .arena_agent import build_arena_agent
from .context import HostedArenaAgentContext
from .memory import HostedAgentRunOutput
from .providers import ProviderErrorCode, ProviderUsage


@dataclass(frozen=True, slots=True)
class HostedAgentRuntimeLimits:
    request_limit: int = 7
    tool_calls_limit: int = 8
    # PydanticAI applies this limit to the whole multi-request Agent run.
    # Individual provider requests remain clamped by
    # arena_action_output_token_budget() in the worker.
    output_tokens_limit: int = 65_536

    def __post_init__(self) -> None:
        if not 1 <= self.request_limit <= 8:
            raise ValueError("request_limit must be between 1 and 8")
        if not 1 <= self.tool_calls_limit <= 16:
            raise ValueError("tool_calls_limit must be between 1 and 16")
        if not 64 <= self.output_tokens_limit <= 65_536:
            raise ValueError(
                "output_tokens_limit must be between 64 and 65536"
            )


@dataclass(frozen=True, slots=True)
class HostedAgentExecution:
    result: AgentTaskResultV1
    agent_output: HostedAgentRunOutput[AgentDrivenMarketActionV1] | None
    usage: ProviderUsage
    request_count: int
    tool_call_count: int
    latency_ms: int
    actual_model: str | None
    error_code: ProviderErrorCode | None


class HostedArenaAgentRuntime:
    """Rehydrate one durable logical Agent and execute one immutable task."""

    def __init__(
        self,
        *,
        model: Model,
        context: HostedArenaAgentContext,
        model_settings: ModelSettings | None = None,
        limits: HostedAgentRuntimeLimits | None = None,
        actual_model: str | None = None,
    ) -> None:
        if context.task.game_agent_id != context.game_memory.state.get(
            "gameAgentId",
            context.task.game_agent_id,
        ):
            raise ValueError("game memory belongs to another Game Agent")
        self._agent = build_arena_agent(model, context.task.kind)
        self._context = context
        self._model_settings = model_settings
        self._limits = limits or HostedAgentRuntimeLimits()
        self._actual_model = actual_model

    async def execute(
        self,
        task_snapshot: ArenaAgentTaskV1,
        deadline: datetime,
    ) -> AgentTaskResultV1:
        return (await self.execute_with_metadata(task_snapshot, deadline)).result

    async def execute_with_metadata(
        self,
        task_snapshot: ArenaAgentTaskV1,
        deadline: datetime,
    ) -> HostedAgentExecution:
        if task_snapshot != self._context.task:
            raise ValueError("Runtime context and task snapshot do not match")
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("deadline must be timezone-aware")
        if sha256_identifier(task_snapshot.input) != task_snapshot.input_hash:
            return self._failed_execution(
                task_snapshot,
                error_code="invalid_structured_output",
            )

        effective_deadline = min(deadline, task_snapshot.deadline_at)
        remaining = (
            effective_deadline - datetime.now(timezone.utc)
        ).total_seconds()
        if remaining <= 0:
            return self._failed_execution(task_snapshot, timed_out=True)

        prompt = (
            "Execute this immutable Arena AgentTask. Use the read-only tools "
            "to inspect the frozen context before choosing the terminal action."
            "\nTask:\n"
            + json.dumps(
                task_snapshot.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                ),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        started = time.monotonic()
        run_usage = RunUsage()
        try:
            async with asyncio.timeout(remaining):
                run = await self._agent.run(
                    prompt,
                    deps=self._context,
                    model_settings=self._model_settings,
                    usage_limits=UsageLimits(
                        request_limit=self._limits.request_limit,
                        tool_calls_limit=self._limits.tool_calls_limit,
                        output_tokens_limit=self._limits.output_tokens_limit,
                    ),
                    usage=run_usage,
                )
        except TimeoutError:
            return self._failed_execution(
                task_snapshot,
                started=started,
                error_code="request_outcome_unknown",
            )
        except ModelHTTPError as exc:
            return self._failed_execution(
                task_snapshot,
                started=started,
                error_code=self._http_error_code(exc.status_code),
            )
        except UsageLimitExceeded as exc:
            return self._failed_execution(
                task_snapshot,
                started=started,
                error_code=self._usage_limit_error_code(exc),
                run_usage=run_usage,
            )
        except UnexpectedModelBehavior:
            return self._failed_execution(
                task_snapshot,
                started=started,
                error_code="invalid_structured_output",
                run_usage=run_usage,
            )
        except AgentRunError:
            return self._failed_execution(
                task_snapshot,
                started=started,
                error_code="request_outcome_unknown",
            )

        usage = run.usage
        normalized_usage = ProviderUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cache_read_tokens,
            reasoning_tokens=int(usage.details.get("reasoning_tokens", 0)),
            complete=True,
        )
        if datetime.now(timezone.utc) >= effective_deadline:
            return HostedAgentExecution(
                result=self._result(task_snapshot, status="timed_out"),
                agent_output=None,
                usage=normalized_usage,
                request_count=usage.requests,
                tool_call_count=usage.tool_calls,
                latency_ms=self._latency_ms(started),
                actual_model=self._actual_model,
                error_code="request_outcome_unknown",
            )
        return HostedAgentExecution(
            result=AgentTaskResultV1(
                result_id=self._result_id(task_snapshot),
                task_id=task_snapshot.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="succeeded",
                action=run.output.action,
            ),
            agent_output=run.output,
            usage=normalized_usage,
            request_count=usage.requests,
            tool_call_count=usage.tool_calls,
            latency_ms=self._latency_ms(started),
            actual_model=self._actual_model,
            error_code=None,
        )

    def _failed_execution(
        self,
        task: ArenaAgentTaskV1,
        *,
        started: float | None = None,
        error_code: ProviderErrorCode | None = None,
        timed_out: bool = False,
        run_usage: RunUsage | None = None,
    ) -> HostedAgentExecution:
        usage = (
            ProviderUsage.incomplete()
            if run_usage is None
            else ProviderUsage(
                input_tokens=run_usage.input_tokens,
                output_tokens=run_usage.output_tokens,
                cached_input_tokens=run_usage.cache_read_tokens,
                reasoning_tokens=int(
                    run_usage.details.get("reasoning_tokens", 0)
                ),
                complete=True,
            )
        )
        return HostedAgentExecution(
            result=self._result(
                task,
                status="timed_out" if timed_out else "failed",
            ),
            agent_output=None,
            usage=usage,
            request_count=0 if run_usage is None else run_usage.requests,
            tool_call_count=0 if run_usage is None else run_usage.tool_calls,
            latency_ms=0 if started is None else self._latency_ms(started),
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
    def _usage_limit_error_code(
        exc: UsageLimitExceeded,
    ) -> ProviderErrorCode:
        if str(exc).startswith(
            "The next request would exceed the request_limit"
        ):
            # Reaching the request cap after the provider returned responses
            # means the bounded Agent run never produced a valid terminal
            # output. The task worker may use its one allowed same-runtime
            # retry while the Arena deadline still has room.
            return "invalid_structured_output"
        return "permanent_request"

    @staticmethod
    def _latency_ms(started: float) -> int:
        return max(0, int((time.monotonic() - started) * 1000))

    @staticmethod
    def _result_id(task: ArenaAgentTaskV1) -> str:
        digest = hashlib.sha256(
            (
                "arena402:pydantic-hosted-result:v1\0"
                f"{task.task_id}\0{task.input_hash}"
            ).encode("utf-8")
        ).hexdigest()
        return f"pydantic-hosted-result-{digest[:40]}"

    @classmethod
    def _result(
        cls,
        task: ArenaAgentTaskV1,
        *,
        status: str,
    ) -> AgentTaskResultV1:
        return AgentTaskResultV1(
            result_id=cls._result_id(task),
            task_id=task.task_id,
            schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
            status=status,
        )


__all__ = [
    "HostedAgentExecution",
    "HostedAgentRuntimeLimits",
    "HostedArenaAgentRuntime",
]
