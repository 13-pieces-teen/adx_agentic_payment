"""Deadline-aware direct model execution for one immutable Arena AgentTask."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol, TypeAlias, cast, runtime_checkable

from pydantic import TypeAdapter, ValidationError

from arena_agent_contracts import (
    AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
    AgentDrivenMarketActionV1,
    AgentTaskResultV1,
    ArenaAgentTaskV1,
    ArenaDecideInputV1,
    ArenaMarketIntentInputV1,
    ArenaMarketRfqInputV1,
    ArenaMarketSelectInputV1,
    ArenaNegotiateInputV1,
    DecideActionV1,
    MarketIntentActionV1,
    MarketRfqActionV1,
    MarketSelectionActionV1,
    NegotiateActionV1,
    RequestNegotiationsActionV1,
)
from arena_core.candidate_validation import (
    CandidateViolation,
    decide_candidate_violation,
    market_intent_candidate_violation,
    market_rfq_candidate_violation,
    market_select_candidate_violation,
    negotiation_candidate_violation,
)
from arena_core.hashing import sha256_identifier

from .capabilities import (
    CapabilityError,
    CapabilityRegistry,
    ResolvedModelCapability,
)
from .prompt_builder import (
    BoundedCorrectionCode,
    BuiltPrompt,
    PromptBuildError,
    PromptBuilder,
)
from .providers import (
    MAX_POSTGRES_BIGINT,
    ProviderAdapter,
    ProviderErrorCode,
    ProviderInvocationError,
    ProviderRequest,
    ProviderResponse,
    ProviderUsage,
    validate_provider_error_code,
    validate_provider_request_id,
)
from .secret_store import (
    SecretReader,
    SecretReference,
    SecretStoreError,
    WorkerSecret,
)


MAX_PROVIDER_ATTEMPTS = 2
DEFAULT_MINIMUM_ATTEMPT_BUDGET_MS = 25

AttemptStatus: TypeAlias = Literal[
    "created",
    "request_sent",
    "succeeded",
    "failed",
    "unknown",
]
AttemptTerminalStatus: TypeAlias = Literal["succeeded", "failed", "unknown"]
AttemptErrorCode: TypeAlias = ProviderErrorCode | Literal[
    "deadline_exceeded",
]
DriverInfrastructureErrorCode: TypeAlias = Literal[
    "attempt_state_unavailable",
]

_DECIDE_ACTION_ADAPTER: TypeAdapter[DecideActionV1] = TypeAdapter(
    DecideActionV1
)
_NEGOTIATE_ACTION_ADAPTER: TypeAdapter[NegotiateActionV1] = TypeAdapter(
    NegotiateActionV1
)
_MARKET_INTENT_ACTION_ADAPTER: TypeAdapter[MarketIntentActionV1] = TypeAdapter(
    MarketIntentActionV1
)
_MARKET_RFQ_ACTION_ADAPTER: TypeAdapter[MarketRfqActionV1] = (
    TypeAdapter(MarketRfqActionV1)
)
_MARKET_SELECT_ACTION_ADAPTER: TypeAdapter[MarketSelectionActionV1] = (
    TypeAdapter(MarketSelectionActionV1)
)


def _require_aware(value: datetime, *, label: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{label} must be an aware datetime")
    return value


def _candidate_violation(
    task: ArenaAgentTaskV1,
    action: AgentDrivenMarketActionV1,
) -> CandidateViolation | None:
    task_input = task.input
    if isinstance(task_input, ArenaMarketIntentInputV1):
        return market_intent_candidate_violation(
            task_input,
            cast(MarketIntentActionV1, action),
        )
    if isinstance(task_input, ArenaMarketRfqInputV1):
        return market_rfq_candidate_violation(
            task_input,
            cast(MarketRfqActionV1, action),
        )
    if isinstance(task_input, ArenaMarketSelectInputV1):
        return market_select_candidate_violation(
            task_input,
            cast(MarketSelectionActionV1, action),
        )
    if isinstance(task_input, ArenaDecideInputV1):
        return decide_candidate_violation(
            task_input,
            cast(DecideActionV1, action),
        )
    if isinstance(task_input, ArenaNegotiateInputV1):
        return negotiation_candidate_violation(
            task_input,
            cast(NegotiateActionV1, action),
        )
    raise TypeError("unsupported Arena task input")


class DirectModelInfrastructureError(RuntimeError):
    """Safe non-result failure for lease/Attempt persistence problems.

    A Worker must not submit an ``AgentTaskResult`` when this is raised. The
    durable queue/recovery layer decides whether the fresh execution can be
    resumed; otherwise Arena's independent Finalizer owns deadline fallback.
    """

    def __init__(self, code: DriverInfrastructureErrorCode) -> None:
        if code != "attempt_state_unavailable":
            raise ValueError(
                "hosted model infrastructure error code is invalid"
            )
        self.code = code
        super().__init__(
            f"Hosted model execution infrastructure failed ({code})"
        )


@dataclass(frozen=True, slots=True)
class DirectModelConfig:
    """Frozen Hosted binding/config selection without raw credentials."""

    provider_id: str
    model_id: str
    credential_ref: SecretReference
    thinking_enabled: bool
    strategy_instructions: str = field(repr=False)
    requested_max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.provider_id) is not str
            or not self.provider_id
            or type(self.model_id) is not str
            or not self.model_id
            or not isinstance(self.credential_ref, SecretReference)
            or type(self.thinking_enabled) is not bool
            or type(self.strategy_instructions) is not str
        ):
            raise ValueError("invalid direct model config")
        if self.requested_max_output_tokens is not None and (
            type(self.requested_max_output_tokens) is not int
            or self.requested_max_output_tokens <= 0
        ):
            raise ValueError("invalid direct model output token limit")


@dataclass(frozen=True, slots=True)
class AttemptCreated:
    """Small private execution record; it contains no Prompt or credential."""

    attempt_id: str
    task_id: str
    attempt_number: int
    provider_id: str
    model_id: str
    thinking_enabled: bool
    created_at: datetime

    def __post_init__(self) -> None:
        if (
            type(self.attempt_id) is not str
            or not self.attempt_id
            or type(self.task_id) is not str
            or not self.task_id
            or type(self.attempt_number) is not int
            or not 1 <= self.attempt_number <= MAX_PROVIDER_ATTEMPTS
            or type(self.provider_id) is not str
            or not self.provider_id
            or type(self.model_id) is not str
            or not self.model_id
            or type(self.thinking_enabled) is not bool
        ):
            raise ValueError("invalid attempt metadata")
        _require_aware(self.created_at, label="created_at")


@dataclass(frozen=True, slots=True)
class AttemptCompletion:
    attempt_id: str
    status: AttemptTerminalStatus
    finished_at: datetime
    latency_ms: int
    usage: ProviderUsage
    provider_request_id: str | None = None
    actual_model: str | None = None
    error_code: AttemptErrorCode | None = None

    def __post_init__(self) -> None:
        if type(self.attempt_id) is not str or not self.attempt_id:
            raise ValueError("invalid attempt completion")
        if self.status not in {"succeeded", "failed", "unknown"}:
            raise ValueError("invalid attempt terminal status")
        _require_aware(self.finished_at, label="finished_at")
        if (
            type(self.latency_ms) is not int
            or self.latency_ms < 0
            or self.latency_ms > MAX_POSTGRES_BIGINT
        ):
            raise ValueError(
                "attempt latency must fit a non-negative PostgreSQL BIGINT"
            )
        if not isinstance(self.usage, ProviderUsage):
            raise ValueError("attempt completion requires normalized usage")
        if self.provider_request_id is not None:
            validate_provider_request_id(self.provider_request_id)
        if self.actual_model is not None and (
            type(self.actual_model) is not str
            or not self.actual_model
            or len(self.actual_model) > 256
        ):
            raise ValueError("invalid actual model")
        if (
            self.error_code is not None
            and self.error_code != "deadline_exceeded"
        ):
            validate_provider_error_code(self.error_code)
        if self.status == "succeeded" and self.error_code is not None:
            raise ValueError("successful attempts cannot have an error code")
        if self.status != "succeeded" and self.error_code is None:
            raise ValueError("failed attempts require an error code")
        if (
            self.status == "unknown"
            and self.error_code != "request_outcome_unknown"
        ):
            raise ValueError("unknown attempts require the unknown error code")


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt_id: str
    task_id: str
    attempt_number: int
    provider_id: str
    model_id: str
    thinking_enabled: bool
    status: AttemptStatus
    created_at: datetime
    request_sent_at: datetime | None
    finished_at: datetime | None
    latency_ms: int | None
    usage: ProviderUsage | None
    provider_request_id: str | None
    actual_model: str | None
    error_code: AttemptErrorCode | None


@runtime_checkable
class AttemptRecorder(Protocol):
    """Persistence port implemented by the Hosted Worker infrastructure.

    Production implementations must be durable. The Driver intentionally does
    not pretend that returning a result also made Attempt metadata durable.
    """

    async def create(self, attempt: AttemptCreated) -> None: ...

    async def mark_request_sent(
        self,
        attempt_id: str,
        *,
        request_sent_at: datetime,
    ) -> None: ...

    async def finish(self, completion: AttemptCompletion) -> None: ...


@dataclass(slots=True)
class _MutableAttempt:
    created: AttemptCreated
    status: AttemptStatus = "created"
    request_sent_at: datetime | None = None
    completion: AttemptCompletion | None = None

    def snapshot(self) -> AttemptRecord:
        completion = self.completion
        return AttemptRecord(
            attempt_id=self.created.attempt_id,
            task_id=self.created.task_id,
            attempt_number=self.created.attempt_number,
            provider_id=self.created.provider_id,
            model_id=self.created.model_id,
            thinking_enabled=self.created.thinking_enabled,
            status=self.status,
            created_at=self.created.created_at,
            request_sent_at=self.request_sent_at,
            finished_at=completion.finished_at if completion else None,
            latency_ms=completion.latency_ms if completion else None,
            usage=completion.usage if completion else None,
            provider_request_id=(
                completion.provider_request_id if completion else None
            ),
            actual_model=(completion.actual_model if completion else None),
            error_code=completion.error_code if completion else None,
        )


class MemoryAttemptRecorder:
    """Test-only in-memory recorder; this is explicitly not durable storage."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._attempts: dict[str, _MutableAttempt] = {}
        self._order: list[str] = []

    @property
    def durable(self) -> Literal[False]:
        return False

    @property
    def records(self) -> tuple[AttemptRecord, ...]:
        # Tests read this after execute has awaited all mutations.
        return tuple(
            self._attempts[attempt_id].snapshot()
            for attempt_id in self._order
        )

    async def create(self, attempt: AttemptCreated) -> None:
        if not isinstance(attempt, AttemptCreated):
            raise ValueError("invalid attempt create record")
        async with self._lock:
            if attempt.attempt_id in self._attempts:
                raise ValueError("duplicate attempt id")
            self._attempts[attempt.attempt_id] = _MutableAttempt(attempt)
            self._order.append(attempt.attempt_id)

    async def mark_request_sent(
        self,
        attempt_id: str,
        *,
        request_sent_at: datetime,
    ) -> None:
        _require_aware(request_sent_at, label="request_sent_at")
        async with self._lock:
            attempt = self._attempts.get(attempt_id)
            if attempt is None or attempt.status != "created":
                raise ValueError("invalid attempt transition")
            attempt.status = "request_sent"
            attempt.request_sent_at = request_sent_at

    async def finish(self, completion: AttemptCompletion) -> None:
        if not isinstance(completion, AttemptCompletion):
            raise ValueError("invalid attempt completion")
        async with self._lock:
            attempt = self._attempts.get(completion.attempt_id)
            if attempt is None or attempt.status != "request_sent":
                raise ValueError("invalid attempt transition")
            attempt.status = completion.status
            attempt.completion = completion


@dataclass(slots=True)
class _ExecutionBudget:
    deadline: datetime
    initial_wall_budget_ms: float
    started_monotonic: float
    wall_clock: Callable[[], datetime]
    monotonic_clock: Callable[[], float]

    @classmethod
    def start(
        cls,
        *,
        deadline: datetime,
        wall_clock: Callable[[], datetime],
        monotonic_clock: Callable[[], float],
    ) -> "_ExecutionBudget":
        now = _require_aware(wall_clock(), label="wall clock")
        started_monotonic = monotonic_clock()
        if type(started_monotonic) not in (int, float):
            raise ValueError("monotonic clock must return a number")
        return cls(
            deadline=deadline,
            initial_wall_budget_ms=max(
                0.0,
                (deadline - now).total_seconds() * 1000,
            ),
            started_monotonic=float(started_monotonic),
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )

    def remaining_ms(self) -> int:
        now = _require_aware(self.wall_clock(), label="wall clock")
        current_monotonic = self.monotonic_clock()
        if type(current_monotonic) not in (int, float):
            return 0
        elapsed_ms = max(
            0.0,
            (float(current_monotonic) - self.started_monotonic) * 1000,
        )
        monotonic_remaining = self.initial_wall_budget_ms - elapsed_ms
        wall_remaining = (self.deadline - now).total_seconds() * 1000
        return max(0, int(min(monotonic_remaining, wall_remaining)))


class DirectModelDriver:
    """Execute one task with one model and at most one retry.

    Retry is limited to transient Provider failures and invalid structured
    output. Authentication/permanent 4xx and unknown post-send outcomes never
    retry. There is no Provider, Model, or Runtime fallback.

    This pure Driver accepts only a fresh, lease-owned execution. Durable
    recovery of pre-existing ``created``/``request_sent`` Attempts belongs to
    the Phase 5 Worker. Attempt persistence failures raise
    ``DirectModelInfrastructureError`` and never fabricate a failed candidate
    result.
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        provider: ProviderAdapter,
        secret_reader: SecretReader,
        config: DirectModelConfig,
        attempt_recorder: AttemptRecorder,
        prompt_builder: PromptBuilder | None = None,
        minimum_attempt_budget_ms: int = (
            DEFAULT_MINIMUM_ATTEMPT_BUDGET_MS
        ),
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(registry, CapabilityRegistry):
            raise TypeError("registry must be CapabilityRegistry")
        if not isinstance(provider, ProviderAdapter):
            raise TypeError("provider must implement ProviderAdapter")
        if not isinstance(config, DirectModelConfig):
            raise TypeError("config must be DirectModelConfig")
        if not isinstance(attempt_recorder, AttemptRecorder):
            raise TypeError("attempt_recorder must implement AttemptRecorder")
        if (
            type(minimum_attempt_budget_ms) is not int
            or minimum_attempt_budget_ms <= 0
        ):
            raise ValueError("minimum attempt budget must be positive")
        self._registry = registry
        self._provider = provider
        self._secret_reader = secret_reader
        self._config = config
        self._attempt_recorder = attempt_recorder
        self._prompt_builder = prompt_builder or PromptBuilder()
        self._minimum_attempt_budget_ms = minimum_attempt_budget_ms
        self._wall_clock = wall_clock or (
            lambda: datetime.now(timezone.utc)
        )
        self._monotonic_clock = monotonic_clock or time.monotonic

    async def execute(
        self,
        task_snapshot: ArenaAgentTaskV1,
        deadline: datetime,
        *,
        first_attempt_number: int = 1,
    ) -> AgentTaskResultV1:
        if not isinstance(task_snapshot, ArenaAgentTaskV1):
            raise TypeError("task_snapshot must be ArenaAgentTaskV1")
        requested_deadline = _require_aware(deadline, label="deadline")
        if (
            type(first_attempt_number) is not int
            or not 1 <= first_attempt_number <= MAX_PROVIDER_ATTEMPTS
        ):
            raise ValueError("first attempt number is invalid")
        if sha256_identifier(task_snapshot.input) != task_snapshot.input_hash:
            return self._result(task_snapshot, status="failed")
        effective_deadline = min(
            requested_deadline,
            task_snapshot.deadline_at,
        )
        budget = _ExecutionBudget.start(
            deadline=effective_deadline,
            wall_clock=self._wall_clock,
            monotonic_clock=self._monotonic_clock,
        )
        if budget.remaining_ms() < self._minimum_attempt_budget_ms:
            return self._result(task_snapshot, status="timed_out")

        try:
            prompt = self._prompt_builder.build(
                task_snapshot,
                strategy_instructions=self._config.strategy_instructions,
            )
        except PromptBuildError:
            return self._result(task_snapshot, status="failed")

        correction_code: BoundedCorrectionCode | None = None
        for attempt_number in range(
            first_attempt_number,
            MAX_PROVIDER_ATTEMPTS + 1,
        ):
            remaining_ms = budget.remaining_ms()
            if remaining_ms < self._minimum_attempt_budget_ms:
                return self._result(
                    task_snapshot,
                    status=(
                        "timed_out" if remaining_ms <= 0 else "failed"
                    ),
                )

            try:
                attempt_prompt = (
                    prompt
                    if correction_code is None
                    else self._prompt_builder.with_bounded_correction(
                        prompt,
                        code=correction_code,
                    )
                )
            except PromptBuildError:
                return self._result(task_snapshot, status="failed")

            try:
                resolved = self._registry.resolve(
                    provider_id=self._config.provider_id,
                    model_id=self._config.model_id,
                    thinking_enabled=self._config.thinking_enabled,
                    remaining_timeout_ms=remaining_ms,
                    requested_max_output_tokens=(
                        self._config.requested_max_output_tokens
                    ),
                )
            except CapabilityError:
                return self._result(task_snapshot, status="failed")
            if resolved.adapter_id != self._provider.adapter_id:
                return self._result(task_snapshot, status="failed")

            credential = await self._resolve_credential(budget)
            if credential is None:
                return self._result(
                    task_snapshot,
                    status=(
                        "timed_out"
                        if budget.remaining_ms() <= 0
                        else "failed"
                    ),
                )

            try:
                remaining_ms = budget.remaining_ms()
                if remaining_ms < self._minimum_attempt_budget_ms:
                    credential.close()
                    return self._result(
                        task_snapshot,
                        status=(
                            "timed_out" if remaining_ms <= 0 else "failed"
                        ),
                    )

                # Resolve again after Secret Store latency so the Provider
                # timeout is bounded by the actual remaining budget.
                resolved = self._registry.resolve(
                    provider_id=self._config.provider_id,
                    model_id=self._config.model_id,
                    thinking_enabled=self._config.thinking_enabled,
                    remaining_timeout_ms=remaining_ms,
                    requested_max_output_tokens=(
                        self._config.requested_max_output_tokens
                    ),
                )
                attempt_id = self._attempt_id(
                    task_snapshot,
                    attempt_number,
                )
                attempt_started_monotonic = self._monotonic_clock()
                created_at = self._aware_now()
                remaining_ms = budget.remaining_ms()
                if remaining_ms <= 0:
                    credential.close()
                    return self._result(
                        task_snapshot,
                        status="timed_out",
                    )

                # Fully validate the safe request before recording
                # request_sent. Recorder I/O is also bounded by the current
                # Arena execution budget.
                request = self._provider_request(
                    task=task_snapshot,
                    attempt_id=attempt_id,
                    resolved=resolved,
                    prompt=attempt_prompt,
                    remaining_ms=remaining_ms,
                )
                await asyncio.wait_for(
                    self._attempt_recorder.create(
                        AttemptCreated(
                            attempt_id=attempt_id,
                            task_id=task_snapshot.task_id,
                            attempt_number=attempt_number,
                            provider_id=resolved.provider_id,
                            model_id=resolved.model_id,
                            thinking_enabled=resolved.thinking_enabled,
                            created_at=created_at,
                        )
                    ),
                    timeout=remaining_ms / 1000,
                )

                remaining_ms = budget.remaining_ms()
                if remaining_ms <= 0:
                    credential.close()
                    return self._result(
                        task_snapshot,
                        status="timed_out",
                    )
                request = self._provider_request(
                    task=task_snapshot,
                    attempt_id=attempt_id,
                    resolved=resolved,
                    prompt=attempt_prompt,
                    remaining_ms=remaining_ms,
                )
                await asyncio.wait_for(
                    self._attempt_recorder.mark_request_sent(
                        attempt_id,
                        request_sent_at=self._aware_now(),
                    ),
                    timeout=remaining_ms / 1000,
                )
            except CapabilityError:
                credential.close()
                return self._result(task_snapshot, status="failed")
            except asyncio.CancelledError:
                credential.close()
                raise
            except TimeoutError:
                # No Provider invocation occurred. A durable recorder may
                # conservatively retain created/request_sent for Phase 5
                # recovery. Never fabricate a competing failed/timed-out
                # candidate result from an uncertain Attempt transition.
                credential.close()
                raise DirectModelInfrastructureError(
                    "attempt_state_unavailable"
                ) from None
            except Exception:
                credential.close()
                raise DirectModelInfrastructureError(
                    "attempt_state_unavailable"
                ) from None

            remaining_ms = budget.remaining_ms()
            if remaining_ms <= 0:
                credential.close()
                return self._result(task_snapshot, status="timed_out")
            request = self._provider_request(
                task=task_snapshot,
                attempt_id=attempt_id,
                resolved=resolved,
                prompt=attempt_prompt,
                remaining_ms=remaining_ms,
            )

            try:
                response = await asyncio.wait_for(
                    self._provider.invoke(request, credential),
                    timeout=request.request_timeout_ms / 1000,
                )
            except TimeoutError:
                # This is the Driver's outer watchdog after request_sent. The
                # adapter can no longer prove whether the Provider processed
                # the request, so it is unknown and must never be replayed.
                await self._finish_attempt(
                    attempt_id=attempt_id,
                    status="unknown",
                    started_monotonic=attempt_started_monotonic,
                    usage=ProviderUsage.incomplete(),
                    error_code="request_outcome_unknown",
                )
                return self._result(task_snapshot, status="failed")
            except ProviderInvocationError as exc:
                should_retry = await self._record_failure(
                    attempt_id=attempt_id,
                    started_monotonic=attempt_started_monotonic,
                    error=exc,
                    budget=budget,
                    attempt_number=attempt_number,
                )
                if should_retry:
                    continue
                return self._terminal_after_failure(task_snapshot, budget)
            except Exception:
                # An unclassified post-send adapter failure has unknown
                # outcome. Never retain its exception and never replay it.
                await self._finish_attempt(
                    attempt_id=attempt_id,
                    status="unknown",
                    started_monotonic=attempt_started_monotonic,
                    usage=ProviderUsage.incomplete(),
                    error_code="request_outcome_unknown",
                )
                return self._result(task_snapshot, status="failed")
            finally:
                credential.close()

            if not isinstance(response, ProviderResponse):
                validation_error = ProviderInvocationError(
                    "invalid_structured_output"
                )
                should_retry = await self._record_failure(
                    attempt_id=attempt_id,
                    started_monotonic=attempt_started_monotonic,
                    error=validation_error,
                    budget=budget,
                    attempt_number=attempt_number,
                )
                if should_retry:
                    continue
                return self._terminal_after_failure(task_snapshot, budget)

            try:
                action = self._parse_action(task_snapshot, response)
            except (ValidationError, ValueError, TypeError):
                validation_error = ProviderInvocationError(
                    "invalid_structured_output"
                )
                should_retry = await self._record_failure(
                    attempt_id=attempt_id,
                    started_monotonic=attempt_started_monotonic,
                    error=validation_error,
                    budget=budget,
                    attempt_number=attempt_number,
                    usage=response.usage,
                    provider_request_id=response.provider_request_id,
                )
                if should_retry:
                    continue
                return self._terminal_after_failure(task_snapshot, budget)

            violation = _candidate_violation(task_snapshot, action)
            if violation is not None:
                validation_error = ProviderInvocationError(
                    "invalid_structured_output"
                )
                should_retry = await self._record_failure(
                    attempt_id=attempt_id,
                    started_monotonic=attempt_started_monotonic,
                    error=validation_error,
                    budget=budget,
                    attempt_number=attempt_number,
                    usage=response.usage,
                    provider_request_id=response.provider_request_id,
                )
                if should_retry:
                    correction_code = (
                        "decision_constraint_violation"
                        if task_snapshot.kind in {
                            "arena.decide",
                            "arena.market.intent",
                            "arena.market.rfq",
                            "arena.market.select",
                        }
                        else (
                            "limit_price_violation"
                            if violation == "limit_price_violation"
                            else "negotiation_rule_violation"
                        )
                    )
                    continue
                return self._terminal_after_failure(task_snapshot, budget)

            await self._finish_attempt(
                attempt_id=attempt_id,
                status="succeeded",
                started_monotonic=attempt_started_monotonic,
                usage=response.usage,
                provider_request_id=response.provider_request_id,
                actual_model=response.actual_model,
            )
            if budget.remaining_ms() <= 0:
                # Arena deadline wins even when a Provider response arrives at
                # the boundary. The Result Sink will independently enforce it.
                return self._result(task_snapshot, status="timed_out")
            return AgentTaskResultV1(
                result_id=self._result_id(task_snapshot),
                task_id=task_snapshot.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="succeeded",
                action=action,
            )

        return self._result(task_snapshot, status="failed")

    async def _resolve_credential(
        self,
        budget: _ExecutionBudget,
    ) -> WorkerSecret | None:
        remaining_ms = budget.remaining_ms()
        if remaining_ms <= 0:
            return None
        try:
            credential = await asyncio.wait_for(
                self._secret_reader.resolve_for_worker(
                    self._config.credential_ref
                ),
                timeout=remaining_ms / 1000,
            )
        except (TimeoutError, SecretStoreError):
            return None
        except Exception:
            # Secret Store ports promise safe exceptions, but an unexpected
            # backend failure must still not surface a raw SDK exception.
            return None
        if not isinstance(credential, WorkerSecret):
            return None
        return credential

    @staticmethod
    def _provider_request(
        *,
        task: ArenaAgentTaskV1,
        attempt_id: str,
        resolved: ResolvedModelCapability,
        prompt: BuiltPrompt,
        remaining_ms: int,
    ) -> ProviderRequest:
        if remaining_ms <= 0:
            raise ValueError("provider request requires remaining budget")
        return ProviderRequest(
            attempt_id=attempt_id,
            task_id=task.task_id,
            task_kind=task.kind,
            idempotency_key=task.idempotency_key,
            model_id=resolved.model_id,
            prompt_version=prompt.prompt_version,
            context_version=prompt.context_version,
            output_version=prompt.output_version,
            system_instructions=prompt.system_instructions,
            input_json=prompt.input_json,
            output_schema_json=prompt.output_schema_json,
            thinking_enabled=resolved.thinking_enabled,
            thinking_parameter_name=resolved.thinking_parameter_name,
            max_output_tokens=resolved.max_output_tokens,
            request_timeout_ms=max(
                1,
                min(resolved.request_timeout_ms, remaining_ms),
            ),
        )

    async def _record_failure(
        self,
        *,
        attempt_id: str,
        started_monotonic: float,
        error: ProviderInvocationError,
        budget: _ExecutionBudget,
        attempt_number: int,
        usage: ProviderUsage | None = None,
        provider_request_id: str | None = None,
    ) -> bool:
        status: AttemptTerminalStatus = (
            "unknown" if error.outcome_unknown else "failed"
        )
        await self._finish_attempt(
            attempt_id=attempt_id,
            status=status,
            started_monotonic=started_monotonic,
            usage=usage or ProviderUsage.incomplete(),
            provider_request_id=provider_request_id,
            error_code=error.code,
        )
        if error.outcome_unknown:
            return False
        retry_class = error.retryable or error.invalid_output
        return (
            retry_class
            and attempt_number < MAX_PROVIDER_ATTEMPTS
            and budget.remaining_ms() >= self._minimum_attempt_budget_ms
        )

    async def _finish_attempt(
        self,
        *,
        attempt_id: str,
        status: AttemptTerminalStatus,
        started_monotonic: float,
        usage: ProviderUsage,
        provider_request_id: str | None = None,
        actual_model: str | None = None,
        error_code: AttemptErrorCode | None = None,
    ) -> None:
        elapsed_ms = max(
            0,
            round(
                (self._monotonic_clock() - started_monotonic)
                * 1000
            ),
        )
        try:
            await self._attempt_recorder.finish(
                AttemptCompletion(
                    attempt_id=attempt_id,
                    status=status,
                    finished_at=self._aware_now(),
                    latency_ms=elapsed_ms,
                    usage=usage,
                    provider_request_id=provider_request_id,
                    actual_model=actual_model,
                    error_code=error_code,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise DirectModelInfrastructureError(
                "attempt_state_unavailable"
            ) from None

    @staticmethod
    def _parse_action(
        task: ArenaAgentTaskV1,
        response: ProviderResponse,
    ) -> AgentDrivenMarketActionV1:
        payload = dict(response.structured_output)
        if task.kind == "arena.decide":
            return _DECIDE_ACTION_ADAPTER.validate_python(
                payload,
                strict=True,
            )
        if task.kind == "arena.negotiate":
            return _NEGOTIATE_ACTION_ADAPTER.validate_python(
                payload,
                strict=True,
            )
        if task.kind == "arena.market.intent":
            return _MARKET_INTENT_ACTION_ADAPTER.validate_python(
                payload,
                strict=True,
            )
        if task.kind == "arena.market.rfq":
            return _MARKET_RFQ_ACTION_ADAPTER.validate_python(
                payload,
                strict=True,
            )
        return _MARKET_SELECT_ACTION_ADAPTER.validate_python(
            payload,
            strict=True,
        )

    def _aware_now(self) -> datetime:
        return _require_aware(self._wall_clock(), label="wall clock")

    @staticmethod
    def _result_id(task: ArenaAgentTaskV1) -> str:
        digest = hashlib.sha256(
            (
                "arena402:hosted-result:v1\0"
                f"{task.task_id}\0{task.input_hash}"
            ).encode("utf-8")
        ).hexdigest()
        return f"hosted-result-{digest[:40]}"

    @staticmethod
    def _attempt_id(
        task: ArenaAgentTaskV1,
        attempt_number: int,
    ) -> str:
        digest = hashlib.sha256(
            (
                "arena402:hosted-attempt:v1\0"
                f"{task.task_id}\0{task.input_hash}"
            ).encode("utf-8")
        ).hexdigest()
        return f"hosted-attempt-{digest[:32]}-{attempt_number}"

    @classmethod
    def _result(
        cls,
        task: ArenaAgentTaskV1,
        *,
        status: Literal["failed", "timed_out"],
    ) -> AgentTaskResultV1:
        return AgentTaskResultV1(
            result_id=cls._result_id(task),
            task_id=task.task_id,
            schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
            status=status,
        )

    @classmethod
    def _terminal_after_failure(
        cls,
        task: ArenaAgentTaskV1,
        budget: _ExecutionBudget,
    ) -> AgentTaskResultV1:
        return cls._result(
            task,
            status=(
                "timed_out" if budget.remaining_ms() <= 0 else "failed"
            ),
        )


__all__ = [
    "AttemptCompletion",
    "AttemptCreated",
    "AttemptRecord",
    "AttemptRecorder",
    "DEFAULT_MINIMUM_ATTEMPT_BUDGET_MS",
    "DirectModelConfig",
    "DirectModelDriver",
    "DirectModelInfrastructureError",
    "MAX_PROVIDER_ATTEMPTS",
    "MemoryAttemptRecorder",
]
