"""DirectModelDriver retry, deadline, thinking, and audit tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from arena_agent_contracts import (
    AGENT_TASK_SCHEMA_VERSION_V1,
    ArenaAgentTaskV1,
)
from arena_core.hashing import sha256_identifier
from hosted_agent_runtime.capabilities import (
    CapabilityRegistry,
    ModelCapability,
    ThinkingMode,
)
from hosted_agent_runtime.direct_model_driver import (
    AttemptCompletion,
    AttemptRecord,
    DirectModelConfig,
    DirectModelDriver,
    DirectModelInfrastructureError,
    MemoryAttemptRecorder,
)
from hosted_agent_runtime.providers import (
    FakeProvider,
    FakeProviderScenario,
    FakeProviderStep,
    MAX_POSTGRES_BIGINT,
    ProviderInvocationError,
    ProviderUsage,
)
from hosted_agent_runtime.prompt_builder import BuiltPrompt
from hosted_agent_runtime.secret_store import (
    MemorySecretStore,
    SecretReference,
    SecretStoreOperationError,
    SecretWrite,
    WorkerSecret,
)
from tests.arena_core_helpers import decide_input, negotiate_input


def _task(
    *,
    negotiate: bool = False,
    deadline: datetime | None = None,
) -> ArenaAgentTaskV1:
    task_deadline = deadline or (
        datetime.now(timezone.utc) + timedelta(seconds=5)
    )
    participant = (
        negotiate_input(deadline=task_deadline)
        if negotiate
        else decide_input(deadline=task_deadline)
    )
    if negotiate:
        idempotency_key = (
            f"{participant.game_id}:{participant.round_id}:"
            f"{participant.negotiation_id}:{participant.turn_sequence}:"
            "game-agent-1:negotiate"
        )
    else:
        idempotency_key = (
            f"{participant.game_id}:{participant.round_id}:"
            "game-agent-1:decide"
        )
    return ArenaAgentTaskV1(
        task_id="task-driver-1",
        kind="arena.negotiate" if negotiate else "arena.decide",
        schema_version=AGENT_TASK_SCHEMA_VERSION_V1,
        game_id=participant.game_id,
        round_id=participant.round_id,
        game_agent_id="game-agent-1",
        negotiation_id=participant.negotiation_id if negotiate else None,
        deadline_at=task_deadline,
        idempotency_key=idempotency_key,
        input_hash=sha256_identifier(participant),
        input=participant,
    )


def _capability(
    *,
    adapter_id: str = "fake-structured",
    thinking_mode: ThinkingMode = ThinkingMode.OPTIONAL,
    thinking_parameter_name: str | None = "thinking.enabled",
    request_timeout_cap_ms: int = 1_000,
) -> ModelCapability:
    return ModelCapability(
        provider_id="fake-provider",
        adapter_id=adapter_id,
        model_id="fake-model-2026-07-24",
        display_name="Fake structured model",
        supports_structured_output=True,
        thinking_mode=thinking_mode,
        thinking_parameter_name=thinking_parameter_name,
        max_output_tokens=512,
        request_timeout_cap_ms=request_timeout_cap_ms,
        adapter_version="fake-adapter-v1",
        immutable_model_id=True,
        verified=True,
        enabled=True,
    )


async def _build_driver(
    steps,
    *,
    thinking_enabled: bool = True,
    capability: ModelCapability | None = None,
    fake_adapter_id: str = "fake-structured",
    recorder: MemoryAttemptRecorder | None = None,
    wall_clock=None,
    monotonic_clock=None,
    on_invoke=None,
    minimum_attempt_budget_ms: int = 25,
):
    store = MemorySecretStore.for_testing()
    secret_ref = SecretReference("arena402/hosted-model/driver-test")
    secret = SecretWrite.from_text("test-only-provider-key")
    try:
        await store.ports.writer.create(secret_ref, secret)
    finally:
        secret.close()
    fake = FakeProvider(
        steps,
        adapter_id=fake_adapter_id,
        on_invoke=on_invoke,
    )
    attempt_recorder = recorder or MemoryAttemptRecorder()
    driver = DirectModelDriver(
        registry=CapabilityRegistry([capability or _capability()]),
        provider=fake,
        secret_reader=store.ports.reader,
        config=DirectModelConfig(
            provider_id="fake-provider",
            model_id="fake-model-2026-07-24",
            credential_ref=secret_ref,
            thinking_enabled=thinking_enabled,
            strategy_instructions="Preserve cash and negotiate conservatively.",
            requested_max_output_tokens=256,
        ),
        attempt_recorder=attempt_recorder,
        minimum_attempt_budget_ms=minimum_attempt_budget_ms,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )
    return driver, fake, attempt_recorder, store, secret_ref


@pytest.mark.parametrize(
    ("scenario", "negotiate", "expected_action"),
    [
        (FakeProviderScenario.BUY, False, "buy"),
        (FakeProviderScenario.SELL, False, "sell"),
        (FakeProviderScenario.PASS, False, "pass"),
        (FakeProviderScenario.PROPOSE, True, "propose"),
        (FakeProviderScenario.ACCEPT, True, "accept"),
        (FakeProviderScenario.REJECT, True, "reject"),
    ],
)
def test_driver_returns_each_strict_action(
    scenario: FakeProviderScenario,
    negotiate: bool,
    expected_action: str,
) -> None:
    async def scenario_run() -> None:
        task = _task(negotiate=negotiate)
        driver, fake, recorder, _, _ = await _build_driver([scenario])
        result = await driver.execute(task, task.deadline_at)

        assert result.status == "succeeded"
        assert result.action is not None
        assert result.action.action == expected_action
        assert len(fake.requests) == 1
        assert len(recorder.records) == 1
        assert recorder.records[0].status == "succeeded"

    asyncio.run(scenario_run())


@pytest.mark.parametrize(
    "first_failure",
    [
        FakeProviderScenario.RATE_LIMITED,
        FakeProviderScenario.SERVER_5XX,
        FakeProviderScenario.TRANSPORT_TIMEOUT,
        FakeProviderScenario.INVALID_JSON,
        FakeProviderScenario.EXTRA_FIELD,
    ],
)
def test_transient_or_invalid_output_retries_once_then_succeeds(
    first_failure: FakeProviderScenario,
) -> None:
    async def scenario_run() -> None:
        task = _task()
        driver, fake, recorder, _, _ = await _build_driver(
            [first_failure, FakeProviderScenario.PASS]
        )
        result = await driver.execute(task, task.deadline_at)

        assert result.status == "succeeded"
        assert result.action is not None
        assert result.action.action == "pass"
        assert len(fake.requests) == 2
        assert [record.attempt_number for record in recorder.records] == [1, 2]
        assert [record.status for record in recorder.records] == [
            "failed",
            "succeeded",
        ]

    asyncio.run(scenario_run())


def test_limit_violating_negotiation_action_gets_one_bounded_correction() -> None:
    async def scenario_run() -> None:
        task = _task(negotiate=True)
        task_input = task.input.model_copy(
            update={"limit_price": Decimal("10.000000")}
        )
        task = task.model_copy(
            update={
                "input": task_input,
                "input_hash": sha256_identifier(task_input),
            }
        )
        driver, fake, recorder, _, _ = await _build_driver(
            [
                FakeProviderScenario.PROPOSE,
                FakeProviderScenario.PROPOSE_AT_LIMIT,
            ]
        )

        result = await driver.execute(task, task.deadline_at)

        assert result.status == "succeeded"
        assert result.action is not None
        assert result.action.action == "propose"
        assert str(result.action.price) == "10.000000"
        assert len(fake.requests) == 2
        correction = json.loads(fake.requests[1].input_json)[
            "boundedCorrection"
        ]
        assert correction == {
            "attempt": 2,
            "code": "limit_price_violation",
        }
        assert [record.status for record in recorder.records] == [
            "failed",
            "succeeded",
        ]

    asyncio.run(scenario_run())


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (FakeProviderScenario.PERMANENT_400, "permanent_request"),
        (
            FakeProviderScenario.AUTHENTICATION_401,
            "authentication_failed",
        ),
    ],
)
def test_permanent_4xx_does_not_retry(
    failure: FakeProviderScenario,
    expected_code: str,
) -> None:
    async def scenario_run() -> None:
        task = _task()
        driver, fake, recorder, _, _ = await _build_driver(
            [failure, FakeProviderScenario.PASS]
        )
        result = await driver.execute(task, task.deadline_at)

        assert result.status == "failed"
        assert len(fake.requests) == 1
        assert len(recorder.records) == 1
        assert recorder.records[0].error_code == expected_code

    asyncio.run(scenario_run())


def test_attempts_are_capped_at_two_without_fallback() -> None:
    async def scenario_run() -> None:
        task = _task()
        driver, fake, recorder, _, _ = await _build_driver(
            [
                FakeProviderScenario.RATE_LIMITED,
                FakeProviderScenario.SERVER_5XX,
                FakeProviderScenario.PASS,
            ]
        )
        result = await driver.execute(task, task.deadline_at)

        assert result.status == "failed"
        assert len(fake.requests) == 2
        assert len(recorder.records) == 2
        assert all(record.status == "failed" for record in recorder.records)

    asyncio.run(scenario_run())


def test_unknown_after_request_sent_is_recorded_and_never_replayed() -> None:
    async def scenario_run() -> None:
        task = _task()
        driver, fake, recorder, _, _ = await _build_driver(
            [
                FakeProviderScenario.UNKNOWN_AFTER_REQUEST_SENT,
                FakeProviderScenario.PASS,
            ]
        )
        result = await driver.execute(task, task.deadline_at)

        assert result.status == "failed"
        assert len(fake.requests) == 1
        assert len(recorder.records) == 1
        record = recorder.records[0]
        assert record.status == "unknown"
        assert record.request_sent_at is not None
        assert record.error_code == "request_outcome_unknown"

    asyncio.run(scenario_run())


def test_usage_missing_remains_none_and_incomplete_in_attempt_audit() -> None:
    async def scenario_run() -> None:
        task = _task()
        driver, _, recorder, _, _ = await _build_driver(
            [FakeProviderScenario.MISSING_USAGE]
        )
        result = await driver.execute(task, task.deadline_at)

        assert result.status == "succeeded"
        usage = recorder.records[0].usage
        assert usage is not None
        assert usage.complete is False
        assert usage.input_tokens is None
        assert usage.output_tokens is None
        assert usage.cached_input_tokens is None
        assert usage.reasoning_tokens is None

    asyncio.run(scenario_run())


def test_provider_reasoning_text_never_reaches_result_or_attempt_record() -> None:
    async def scenario_run() -> None:
        task = _task()
        driver, _, recorder, _, _ = await _build_driver(
            [FakeProviderScenario.REASONING_TEXT]
        )
        result = await driver.execute(task, task.deadline_at)

        assert result.status == "succeeded"
        assert result.model_dump(mode="json")["action"] == {
            "action": "pass"
        }
        attempt_field_names = {
            field.name.casefold() for field in fields(AttemptRecord)
        }
        assert not any(
            marker in name
            for name in attempt_field_names
            for marker in ("reasoning_text", "reasoning_content", "thought")
        )
        assert recorder.records[0].status == "succeeded"

    asyncio.run(scenario_run())


def test_thinking_is_resolved_by_capability_registry() -> None:
    async def scenario_run() -> None:
        task = _task()
        driver, fake, recorder, _, _ = await _build_driver(
            [FakeProviderScenario.PASS],
            thinking_enabled=True,
        )
        result = await driver.execute(task, task.deadline_at)

        assert result.status == "succeeded"
        assert fake.requests[0].thinking_enabled is True
        assert fake.requests[0].thinking_parameter_name == "thinking.enabled"
        assert recorder.records[0].thinking_enabled is True

    asyncio.run(scenario_run())


def test_registry_rejection_stops_before_secret_or_provider_attempt() -> None:
    async def scenario_run() -> None:
        task = _task()
        unsupported = _capability(
            thinking_mode=ThinkingMode.UNSUPPORTED,
            thinking_parameter_name=None,
        )
        driver, fake, recorder, _, _ = await _build_driver(
            [FakeProviderScenario.PASS],
            thinking_enabled=True,
            capability=unsupported,
        )
        result = await driver.execute(task, task.deadline_at)

        assert result.status == "failed"
        assert fake.requests == ()
        assert recorder.records == ()

    asyncio.run(scenario_run())


def test_adapter_mismatch_does_not_fallback() -> None:
    async def scenario_run() -> None:
        task = _task()
        driver, fake, recorder, _, _ = await _build_driver(
            [FakeProviderScenario.PASS],
            capability=_capability(adapter_id="required-adapter"),
            fake_adapter_id="different-adapter",
        )
        result = await driver.execute(task, task.deadline_at)

        assert result.status == "failed"
        assert fake.requests == ()
        assert recorder.records == ()

    asyncio.run(scenario_run())


def test_revoked_secret_fails_closed_before_attempt() -> None:
    async def scenario_run() -> None:
        task = _task()
        driver, fake, recorder, store, secret_ref = await _build_driver(
            [FakeProviderScenario.PASS]
        )
        await store.ports.controller.revoke(secret_ref)
        result = await driver.execute(task, task.deadline_at)

        assert result.status == "failed"
        assert fake.requests == ()
        assert recorder.records == ()

    asyncio.run(scenario_run())


def test_naive_deadline_is_rejected_and_task_deadline_cannot_be_extended() -> None:
    async def scenario_run() -> None:
        task = _task()
        driver, fake, recorder, _, _ = await _build_driver(
            [FakeProviderScenario.PASS]
        )
        with pytest.raises(ValueError, match="aware datetime"):
            await driver.execute(task, datetime(2026, 7, 24, 12, 0))

        expired = _task(
            deadline=datetime.now(timezone.utc) - timedelta(milliseconds=1)
        )
        result = await driver.execute(
            expired,
            datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        assert result.status == "timed_out"
        assert fake.requests == ()
        assert recorder.records == ()

    asyncio.run(scenario_run())


def test_driver_rejects_task_whose_frozen_input_hash_does_not_match() -> None:
    async def scenario_run() -> None:
        task = _task().model_copy(
            update={"input_hash": f"sha256:{'0' * 64}"}
        )
        driver, fake, recorder, _, _ = await _build_driver(
            [FakeProviderScenario.PASS]
        )
        result = await driver.execute(task, task.deadline_at)

        assert result.status == "failed"
        assert fake.requests == ()
        assert recorder.records == ()

    asyncio.run(scenario_run())


class _MutableClock:
    def __init__(self) -> None:
        self.wall = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        self.monotonic_value = 10.0

    def wall_now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.monotonic_value

    def advance_monotonic(self, milliseconds: int) -> None:
        self.monotonic_value += milliseconds / 1000

    def advance_wall(self, milliseconds: int) -> None:
        self.wall += timedelta(milliseconds=milliseconds)


@pytest.mark.parametrize(
    ("advance_ms", "expected_status"),
    [(90, "failed"), (110, "timed_out")],
)
def test_monotonic_budget_stops_retry_when_budget_is_insufficient(
    advance_ms: int,
    expected_status: str,
) -> None:
    async def scenario_run() -> None:
        clock = _MutableClock()
        task = _task(deadline=clock.wall + timedelta(milliseconds=100))
        driver, fake, recorder, _, _ = await _build_driver(
            [
                FakeProviderScenario.RATE_LIMITED,
                FakeProviderScenario.PASS,
            ],
            wall_clock=clock.wall_now,
            monotonic_clock=clock.monotonic,
            on_invoke=lambda _: clock.advance_monotonic(advance_ms),
        )
        result = await driver.execute(task, task.deadline_at)

        assert result.status == expected_status
        assert len(fake.requests) == 1
        assert len(recorder.records) == 1
        assert recorder.records[0].latency_ms == advance_ms

    asyncio.run(scenario_run())


def test_wall_clock_budget_also_stops_retry() -> None:
    async def scenario_run() -> None:
        clock = _MutableClock()
        task = _task(deadline=clock.wall + timedelta(milliseconds=100))
        driver, fake, recorder, _, _ = await _build_driver(
            [
                FakeProviderScenario.RATE_LIMITED,
                FakeProviderScenario.PASS,
            ],
            wall_clock=clock.wall_now,
            monotonic_clock=clock.monotonic,
            on_invoke=lambda _: clock.advance_wall(110),
        )
        result = await driver.execute(task, task.deadline_at)

        assert result.status == "timed_out"
        assert len(fake.requests) == 1
        assert len(recorder.records) == 1

    asyncio.run(scenario_run())


def test_outer_watchdog_timeout_after_request_sent_is_unknown_no_retry() -> None:
    async def scenario_run() -> None:
        task = _task()
        driver, fake, recorder, _, _ = await _build_driver(
            [
                FakeProviderStep(
                    FakeProviderScenario.PASS,
                    delay_ms=30,
                ),
                FakeProviderScenario.PASS,
            ],
            capability=_capability(request_timeout_cap_ms=5),
        )
        result = await driver.execute(task, task.deadline_at)

        assert result.status == "failed"
        assert len(fake.requests) == 1
        assert len(recorder.records) == 1
        assert recorder.records[0].status == "unknown"
        assert recorder.records[0].error_code == "request_outcome_unknown"

    asyncio.run(scenario_run())


def test_result_id_is_stable_across_equivalent_driver_execution() -> None:
    async def scenario_run() -> None:
        task = _task()
        first, _, _, _, _ = await _build_driver(
            [FakeProviderScenario.PASS]
        )
        second, _, _, _, _ = await _build_driver(
            [FakeProviderScenario.PASS]
        )
        first_result = await first.execute(task, task.deadline_at)
        second_result = await second.execute(task, task.deadline_at)

        assert first_result.result_id == second_result.result_id
        assert first_result.result_id.startswith("hosted-result-")

    asyncio.run(scenario_run())


def test_memory_attempt_recorder_is_explicitly_not_durable() -> None:
    recorder = MemoryAttemptRecorder()
    assert recorder.durable is False
    assert recorder.records == ()


class _TrackingSecretReader:
    def __init__(self) -> None:
        self.secret = WorkerSecret(b"test-only-tracked-secret")

    async def resolve_for_worker(
        self,
        secret_ref: SecretReference,
    ) -> WorkerSecret:
        return self.secret


class _FailingAttemptRecorder:
    def __init__(self, stage: str) -> None:
        self.stage = stage

    async def create(self, attempt) -> None:
        if self.stage == "create":
            raise RuntimeError("test recorder unavailable")

    async def mark_request_sent(
        self,
        attempt_id: str,
        *,
        request_sent_at: datetime,
    ) -> None:
        if self.stage == "mark":
            raise RuntimeError("test recorder unavailable")

    async def finish(self, completion) -> None:
        return None


class _SlowAttemptRecorder(MemoryAttemptRecorder):
    def __init__(self, stage: str, *, delay_seconds: float) -> None:
        super().__init__()
        self.stage = stage
        self.delay_seconds = delay_seconds

    async def create(self, attempt) -> None:
        if self.stage == "create":
            await asyncio.sleep(self.delay_seconds)
        await super().create(attempt)

    async def mark_request_sent(
        self,
        attempt_id: str,
        *,
        request_sent_at: datetime,
    ) -> None:
        if self.stage == "mark":
            await asyncio.sleep(self.delay_seconds)
        await super().mark_request_sent(
            attempt_id,
            request_sent_at=request_sent_at,
        )


class _InvalidPromptBuilder:
    def build(self, task, *, strategy_instructions: str) -> BuiltPrompt:
        return BuiltPrompt(
            prompt_version="arena.hosted-prompt.v1",
            context_version="arena.agent-task.v1",
            output_version="arena.agent-action.v1",
            system_instructions="",
            input_json="{}",
            output_schema_json="{}",
        )


@pytest.mark.parametrize("failure_stage", ["request", "create", "mark"])
def test_pre_send_failure_always_closes_worker_secret(
    failure_stage: str,
) -> None:
    async def scenario_run() -> None:
        task = _task()
        reader = _TrackingSecretReader()
        fake = FakeProvider([FakeProviderScenario.PASS])
        recorder = _FailingAttemptRecorder(failure_stage)
        driver = DirectModelDriver(
            registry=CapabilityRegistry([_capability()]),
            provider=fake,
            secret_reader=reader,
            config=DirectModelConfig(
                provider_id="fake-provider",
                model_id="fake-model-2026-07-24",
                credential_ref=SecretReference(
                    "arena402/hosted-model/tracked-test"
                ),
                thinking_enabled=True,
                strategy_instructions="Conservative.",
            ),
            attempt_recorder=recorder,
            prompt_builder=(
                _InvalidPromptBuilder()
                if failure_stage == "request"
                else None
            ),
        )
        with pytest.raises(DirectModelInfrastructureError):
            await driver.execute(task, task.deadline_at)
        assert fake.requests == ()
        with pytest.raises(SecretStoreOperationError) as exc:
            reader.secret.reveal_for_worker()
        assert exc.value.code == "secret_value_closed"

    asyncio.run(scenario_run())


@pytest.mark.parametrize("slow_stage", ["create", "mark"])
def test_recorder_io_cannot_send_provider_request_after_deadline(
    slow_stage: str,
) -> None:
    async def scenario_run() -> None:
        task = _task(
            deadline=datetime.now(timezone.utc) + timedelta(milliseconds=30)
        )
        reader = _TrackingSecretReader()
        fake = FakeProvider([FakeProviderScenario.PASS])
        recorder = _SlowAttemptRecorder(
            slow_stage,
            delay_seconds=0.1,
        )
        driver = DirectModelDriver(
            registry=CapabilityRegistry([_capability()]),
            provider=fake,
            secret_reader=reader,
            config=DirectModelConfig(
                provider_id="fake-provider",
                model_id="fake-model-2026-07-24",
                credential_ref=SecretReference(
                    "arena402/hosted-model/slow-recorder-test"
                ),
                thinking_enabled=True,
                strategy_instructions="Conservative.",
            ),
            attempt_recorder=recorder,
            minimum_attempt_budget_ms=1,
        )

        with pytest.raises(DirectModelInfrastructureError) as exc:
            await driver.execute(task, task.deadline_at)
        assert exc.value.code == "attempt_state_unavailable"
        assert fake.requests == ()
        with pytest.raises(SecretStoreOperationError) as exc:
            reader.secret.reveal_for_worker()
        assert exc.value.code == "secret_value_closed"

    asyncio.run(scenario_run())


def test_duplicate_execution_never_fabricates_a_competing_failed_result() -> None:
    async def scenario_run() -> None:
        task = _task()
        driver, fake, recorder, _, _ = await _build_driver(
            [FakeProviderScenario.PASS]
        )
        first = await driver.execute(task, task.deadline_at)
        assert first.status == "succeeded"

        with pytest.raises(DirectModelInfrastructureError) as exc:
            await driver.execute(task, task.deadline_at)

        assert exc.value.code == "attempt_state_unavailable"
        assert len(fake.requests) == 1
        assert len(recorder.records) == 1
        assert recorder.records[0].status == "succeeded"

    asyncio.run(scenario_run())


def test_attempt_completion_rejects_secret_shaped_provider_request_id() -> None:
    unsafe_id = "sk-abcdefghijklmnop"
    with pytest.raises(ValueError) as exc:
        AttemptCompletion(
            attempt_id="attempt-1",
            status="succeeded",
            finished_at=datetime.now(timezone.utc),
            latency_ms=1,
            usage=ProviderUsage.incomplete(),
            provider_request_id=unsafe_id,
        )
    assert unsafe_id not in str(exc.value)


@pytest.mark.parametrize(
    "unsafe_code",
    [
        "sk-abcdefghijklmnop",
        "provider failed\nAuthorization: Bearer secret",
        "new_provider_error",
    ],
)
def test_provider_error_codes_are_a_runtime_closed_set(
    unsafe_code: str,
) -> None:
    with pytest.raises(ValueError) as invocation_error:
        ProviderInvocationError(unsafe_code)  # type: ignore[arg-type]
    assert unsafe_code not in str(invocation_error.value)

    with pytest.raises(ValueError) as completion_error:
        AttemptCompletion(
            attempt_id="attempt-1",
            status="failed",
            finished_at=datetime.now(timezone.utc),
            latency_ms=1,
            usage=ProviderUsage.incomplete(),
            error_code=unsafe_code,  # type: ignore[arg-type]
        )
    assert unsafe_code not in str(completion_error.value)


def test_driver_infrastructure_error_code_is_a_runtime_closed_set() -> None:
    unsafe_code = "sk-abcdefghijklmnop"
    with pytest.raises(ValueError) as exc:
        DirectModelInfrastructureError(  # type: ignore[arg-type]
            unsafe_code
        )
    assert unsafe_code not in str(exc.value)


@pytest.mark.parametrize(
    "too_large",
    [MAX_POSTGRES_BIGINT + 1, 10**100],
)
def test_attempt_usage_and_latency_must_fit_postgresql_bigint(
    too_large: int,
) -> None:
    with pytest.raises(ValueError):
        ProviderUsage(
            input_tokens=too_large,
            output_tokens=0,
            cached_input_tokens=0,
            reasoning_tokens=0,
            complete=True,
        )

    with pytest.raises(ValueError):
        AttemptCompletion(
            attempt_id="attempt-1",
            status="succeeded",
            finished_at=datetime.now(timezone.utc),
            latency_ms=too_large,
            usage=ProviderUsage.incomplete(),
        )
