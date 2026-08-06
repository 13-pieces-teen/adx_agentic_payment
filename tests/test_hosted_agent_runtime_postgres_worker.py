from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic_ai import models
from pydantic_ai.models.test import TestModel

from arena_agent_contracts import (
    AGENT_TASK_SCHEMA_VERSION_V1,
    AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
    AgentTaskResultV1,
    ArenaAgentTaskV1,
    BuyAction,
)
from arena_core.hashing import sha256_identifier
from hosted_agent_runtime.capabilities import CapabilityRegistry
from hosted_agent_runtime.postgres_worker import (
    ClaimedLearningJob,
    ClaimedTask,
    ClaimedValidation,
    DurableHostedWorker,
    PostgresHostedWorkerRepository,
    arena_action_output_token_budget,
)
from hosted_agent_runtime.production_providers import ProductionProviderBundle
from hosted_agent_runtime.providers import (
    ProviderInvocationError,
    ProviderRequest,
    ProviderResponse,
    ProviderUsage,
)
from hosted_agent_runtime.secret_store import (
    SecretStoreOperationError,
    WorkerSecret,
)
from tests.arena_core_helpers import decide_input


models.ALLOW_MODEL_REQUESTS = False


def _job() -> ClaimedValidation:
    return ClaimedValidation(
        validation_job_id="validation-1",
        candidate_config_hash="sha256:" + "a" * 64,
        expected_current_config_hash="sha256:" + "b" * 64,
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        provider="deepseek",
        model="deepseek-v4-flash",
        thinking_enabled=True,
        secret_ref="arena402/hosted-model/credential-1",
    )


def test_arena_action_output_budget_is_small_and_thinking_aware() -> None:
    assert arena_action_output_token_budget(
        configured_tokens=16_384,
        thinking_enabled=False,
    ) == 8_192
    assert arena_action_output_token_budget(
        configured_tokens=16_384,
        thinking_enabled=True,
    ) == 16_384
    assert arena_action_output_token_budget(
        configured_tokens=128,
        thinking_enabled=False,
    ) == 128


class _Repository:
    def __init__(self) -> None:
        self.completions: list[dict[str, object]] = []
        self.claim_order: list[str] = []

    async def start_validation(
        self,
        worker_id: str,
        validation_job_id: str,
    ) -> int:
        del worker_id, validation_job_id
        return 1

    async def complete_validation(
        self,
        worker_id: str,
        job: ClaimedValidation,
        **values: object,
    ) -> str:
        del worker_id, job
        self.completions.append(values)
        return str(values["outcome"])

    async def claim_tasks(self, *_: object, **__: object) -> tuple[()]:
        self.claim_order.append("tasks")
        return ()

    async def claim_validations(
        self,
        *_: object,
        **__: object,
    ) -> tuple[()]:
        self.claim_order.append("validations")
        return ()

    async def project_memory_patches(self, *, limit: int) -> int:
        assert limit == 100
        return 0

    async def claim_learning_jobs(
        self,
        *_: object,
        **__: object,
    ) -> tuple[()]:
        self.claim_order.append("learning")
        return ()


class _Reader:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.resolved: WorkerSecret | None = None

    async def resolve_for_worker(self, _: object) -> WorkerSecret:
        if self.fails:
            raise SecretStoreOperationError("backend_unavailable")
        self.resolved = WorkerSecret(b"test-provider-key")
        return self.resolved


class _Adapter:
    adapter_id = "deepseek-openai-chat-v1"

    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.requests: list[ProviderRequest] = []

    async def invoke(
        self,
        request: ProviderRequest,
        _: object,
    ) -> ProviderResponse:
        self.requests.append(request)
        if self.error is not None:
            raise ProviderInvocationError(self.error)  # type: ignore[arg-type]
        return ProviderResponse(
            structured_output={"ok": True},
            usage=ProviderUsage(
                input_tokens=1,
                output_tokens=1,
                cached_input_tokens=0,
                reasoning_tokens=0,
                complete=True,
            ),
        )


def _worker(
    repository: _Repository,
    reader: _Reader,
    adapter: _Adapter,
) -> DurableHostedWorker:
    return DurableHostedWorker(
        repository=repository,  # type: ignore[arg-type]
        providers=ProductionProviderBundle(
            registry=CapabilityRegistry(),
            adapters={"deepseek": adapter},  # type: ignore[dict-item]
        ),
        secret_reader=reader,
        worker_id="worker-1",
    )


def test_validation_success_closes_secret_and_marks_ready() -> None:
    repository = _Repository()
    reader = _Reader()
    adapter = _Adapter()
    worker = _worker(repository, reader, adapter)

    asyncio.run(worker._validate(_job()))

    assert repository.completions == [{"outcome": "succeeded"}]
    assert len(adapter.requests) == 1
    validation_request = adapter.requests[0]
    assert validation_request.thinking_enabled is False
    assert validation_request.thinking_parameter_name is None
    assert validation_request.max_output_tokens == 128
    assert '{"ok":true}' in validation_request.system_instructions
    assert reader.resolved is not None
    with pytest.raises(SecretStoreOperationError) as exc:
        reader.resolved.reveal_for_worker()
    assert exc.value.code == "secret_value_closed"


def test_validation_authentication_failure_is_permanent() -> None:
    repository = _Repository()
    worker = _worker(
        repository,
        _Reader(),
        _Adapter("authentication_failed"),
    )

    asyncio.run(worker._validate(_job()))

    assert repository.completions == [
        {
            "outcome": "permanent_failure",
            "error_class": "authentication_failed",
            "retry_at": None,
        }
    ]


def test_validation_invalid_output_uses_bounded_job_retry() -> None:
    repository = _Repository()
    worker = _worker(
        repository,
        _Reader(),
        _Adapter("invalid_structured_output"),
    )

    asyncio.run(worker._validate(_job()))

    assert len(repository.completions) == 1
    completion = repository.completions[0]
    assert completion["outcome"] == "transient_failure"
    assert completion["error_class"] == "invalid_structured_output"
    assert isinstance(completion["retry_at"], datetime)


def test_validation_secret_outage_is_durable_transient_failure() -> None:
    repository = _Repository()
    worker = _worker(repository, _Reader(fails=True), _Adapter())

    asyncio.run(worker._validate(_job()))

    assert len(repository.completions) == 1
    completion = repository.completions[0]
    assert completion["outcome"] == "transient_failure"
    assert completion["error_class"] == "provider_unavailable"
    assert isinstance(completion["retry_at"], datetime)


def test_worker_prioritizes_arena_tasks_before_validation_jobs() -> None:
    repository = _Repository()
    worker = _worker(repository, _Reader(), _Adapter())

    assert asyncio.run(worker.run_once()) == 0
    assert repository.claim_order == ["tasks", "validations", "learning"]


@pytest.mark.parametrize(
    ("recovery_disposition", "expected_error"),
    [
        ("terminal_unknown", "request_outcome_unknown"),
        ("terminal_failed", "attempts_exhausted"),
    ],
)
def test_reclaimed_terminal_task_never_replays_provider_request(
    recovery_disposition: str,
    expected_error: str,
) -> None:
    class _RecoveryRepository:
        def __init__(self) -> None:
            self.submitted: list[tuple[AgentTaskResultV1, str | None]] = []

        async def submit_result(
            self,
            worker_id: str,
            result: AgentTaskResultV1,
            *,
            error_class: str | None,
            **_: object,
        ) -> str:
            assert worker_id == "worker-recovery"
            self.submitted.append((result, error_class))
            return "accepted"

    class _ForbiddenModelFactory:
        def build(self, **_: object) -> object:
            raise AssertionError("recovery must not replay the Provider")

    async def scenario() -> None:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=10)
        participant = decide_input(deadline=deadline)
        task = ArenaAgentTaskV1(
            task_id=f"task-{recovery_disposition}",
            kind="arena.decide",
            schema_version=AGENT_TASK_SCHEMA_VERSION_V1,
            game_id=participant.game_id,
            round_id=participant.round_id,
            game_agent_id="game-agent-recovery",
            deadline_at=deadline,
            idempotency_key=(
                f"{participant.game_id}:{participant.round_id}:"
                "game-agent-recovery:decide"
            ),
            input_hash=sha256_identifier(participant),
            input=participant,
        )
        claimed = ClaimedTask(
            task=task,
            deadline_at=deadline,
            provider="official-deepseek",
            secret_ref="arena402/hosted-model/credential-recovery",
            runtime_config={
                "model_id": "deepseek-v4-flash",
                "thinking_enabled": False,
                "max_output_tokens": 8192,
            },
            attempt_count=1,
            recovery_disposition=recovery_disposition,
            first_attempt_number=None,
        )
        repository = _RecoveryRepository()
        worker = DurableHostedWorker(
            repository=repository,  # type: ignore[arg-type]
            providers=ProductionProviderBundle(
                registry=CapabilityRegistry(),
                adapters={},
            ),
            secret_reader=_Reader(),
            worker_id="worker-recovery",
            model_factory=_ForbiddenModelFactory(),  # type: ignore[arg-type]
        )

        await worker._execute_task(claimed)

        assert len(repository.submitted) == 1
        result, error_class = repository.submitted[0]
        assert result.status == "failed"
        assert result.action is None
        assert error_class == expected_error

    asyncio.run(scenario())


def test_postgres_result_sink_receives_candidate_action_as_json_object() -> None:
    class _Pool:
        def __init__(self) -> None:
            self.parameters: tuple[object, ...] | None = None

        async def fetchrow(
            self,
            _: str,
            *parameters: object,
        ) -> dict[str, str]:
            self.parameters = parameters
            return {"disposition": "accepted"}

    async def scenario() -> None:
        pool = _Pool()
        repository = PostgresHostedWorkerRepository(
            "",
            pool=pool,
        )
        result = AgentTaskResultV1(
            result_id="result-1",
            task_id="task-1",
            schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
            status="succeeded",
            action=BuyAction(action="buy", good="iron"),
        )

        assert (
            await repository.submit_result(
                "worker-1",
                result,
                message_replaced=False,
                policy_version=None,
                error_class=None,
            )
            == "accepted"
        )
        assert pool.parameters is not None
        assert pool.parameters[6] == {"action": "buy", "good": "iron"}

    asyncio.run(scenario())


def test_worker_retries_invalid_pydantic_output_and_stages_winner_memory() -> None:
    class _AgentRepository:
        def __init__(self) -> None:
            self.attempts: list[object] = []
            self.completions: list[object] = []
            self.staged: list[dict[str, object]] = []
            self.results: list[AgentTaskResultV1] = []

        async def load_runtime_context(
            self,
            worker_id: str,
            task_id: str,
        ) -> dict[str, object]:
            assert worker_id == "worker-pydantic"
            assert task_id == "task-worker-pydantic"
            return {
                "agentId": "agent-official-1",
                "gameAgentId": "game-agent-1",
                "strategyRevisionId": "strategy:official-1",
                "strategyRevisionNo": 3,
                "strategyArchetype": "aggressive",
                "strategyCatalogVersion": "arena.hosted-strategy.v1",
                "strategyInstructions": (
                    "Seek bounded upside while preserving hard limits."
                ),
                "memoryVersion": 4,
                "memoryState": {
                    "schemaVersion": "arena.hosted-game-memory.v1",
                    "gameAgentId": "game-agent-1",
                },
            }

        async def start_attempt(
            self,
            worker_id: str,
            attempt: object,
        ) -> int:
            assert worker_id == "worker-pydantic"
            self.attempts.append(attempt)
            return len(self.attempts)

        async def mark_attempt_sent(
            self,
            worker_id: str,
            attempt_id: str,
        ) -> bool:
            assert worker_id == "worker-pydantic"
            assert attempt_id.startswith("pydantic-hosted-attempt-")
            return True

        async def finish_pydantic_attempt(
            self,
            worker_id: str,
            completion: object,
            **counts: object,
        ) -> bool:
            assert worker_id == "worker-pydantic"
            if completion.status == "succeeded":
                assert int(counts["request_count"]) >= 2
                assert int(counts["tool_call_count"]) >= 1
            self.completions.append(completion)
            return True

        async def stage_memory_patch(
            self,
            worker_id: str,
            task_id: str,
            **values: object,
        ) -> bool:
            assert worker_id == "worker-pydantic"
            assert task_id == "task-worker-pydantic"
            self.staged.append(values)
            return True

        async def submit_result(
            self,
            worker_id: str,
            result: AgentTaskResultV1,
            **_: object,
        ) -> str:
            assert worker_id == "worker-pydantic"
            self.results.append(result)
            return "accepted"

    class _BuiltModel:
        def __init__(self, action: dict[str, object]) -> None:
            self.model = TestModel(
                custom_output_args={
                    "action": action,
                    "decision_summary": {
                        "plan": "Wait for a stronger bounded opportunity.",
                        "factors": ["Current edge is insufficient."],
                        "confidence_bps": 7100,
                    },
                    "memory_patch": {
                        "round_summary": "Reviewed the frozen market state.",
                        "next_plan": "Re-check prices in the next round.",
                        "observations": ["No legal trade has enough edge."],
                        "strategy_adjustments": [],
                        "risk_budget_bps": 5000,
                    },
                }
            )
            self.settings = None
            self.resolved = SimpleNamespace(
                provider_id="official-deepseek",
                model_id="deepseek-v4-flash",
                thinking_enabled=True,
            )
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class _ModelFactory:
        def __init__(self) -> None:
            self.built: list[_BuiltModel] = []
            self.values: list[dict[str, object]] = []

        def build(self, **values: object) -> _BuiltModel:
            self.values.append(values)
            action = (
                {
                    "action": "buy",
                    "good": "iron",
                    "limitPrice": "1.000000",
                }
                if not self.built
                else {"action": "pass"}
            )
            built = _BuiltModel(action)
            self.built.append(built)
            return built

    async def scenario() -> None:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=10)
        participant = decide_input(deadline=deadline)
        task = ArenaAgentTaskV1(
            task_id="task-worker-pydantic",
            kind="arena.decide",
            schema_version=AGENT_TASK_SCHEMA_VERSION_V1,
            game_id=participant.game_id,
            round_id=participant.round_id,
            game_agent_id="game-agent-1",
            deadline_at=deadline,
            idempotency_key="game-1:round-1:game-agent-1:decide",
            input_hash=sha256_identifier(participant),
            input=participant,
        )
        claimed = ClaimedTask(
            task=task,
            deadline_at=deadline,
            provider="official-deepseek",
            secret_ref="arena402/hosted-model/credential-1",
            runtime_config={
                "model_id": "deepseek-v4-flash",
                "thinking_enabled": True,
                "max_output_tokens": 16_384,
                "strategy_instructions": "Frozen in the strategy revision.",
            },
            attempt_count=0,
            recovery_disposition="execute",
            first_attempt_number=1,
        )
        repository = _AgentRepository()
        reader = _Reader()
        model_factory = _ModelFactory()
        worker = DurableHostedWorker(
            repository=repository,  # type: ignore[arg-type]
            providers=ProductionProviderBundle(
                registry=CapabilityRegistry(),
                adapters={},
            ),
            secret_reader=reader,
            worker_id="worker-pydantic",
            model_factory=model_factory,  # type: ignore[arg-type]
        )

        await worker._execute_task(claimed)

        assert len(repository.attempts) == 2
        assert len(repository.completions) == 2
        assert repository.completions[0].status == "failed"
        assert (
            repository.completions[0].error_code
            == "invalid_structured_output"
        )
        assert repository.completions[1].status == "succeeded"
        assert repository.staged[0]["expected_memory_version"] == 4
        assert str(
            repository.staged[0]["runtime_result_id_digest"]
        ).startswith("sha256:")
        assert repository.results[0].status == "succeeded"
        assert repository.results[0].action is not None
        assert repository.results[0].action.action == "pass"
        assert len(model_factory.values) == 2
        assert all(
            values["api_key"] == "test-provider-key"
            for values in model_factory.values
        )
        assert all(built.closed for built in model_factory.built)
        assert reader.resolved is not None
        with pytest.raises(SecretStoreOperationError):
            reader.resolved.reveal_for_worker()

    asyncio.run(scenario())


def test_worker_learns_completed_game_and_submits_gated_revision() -> None:
    class _LearningRepository:
        def __init__(self) -> None:
            self.completion: dict[str, object] | None = None
            self.releases: list[dict[str, object]] = []

        async def load_learning_evidence(
            self,
            worker_id: str,
            learning_job_id: str,
        ) -> dict[str, object]:
            assert worker_id == "worker-learning"
            assert learning_job_id == "learning:test-1"
            return {
                "schemaVersion": "arena.hosted-learning-evidence.v2",
                "learningJobId": learning_job_id,
                "gameId": "game-1",
                "gameAgentId": "game-agent-1",
                "agentId": "agent-1",
                "baseStrategyRevisionId": "strategy:test-1",
                "baseStrategyRevisionNo": 1,
                "archetype": "balanced",
                "catalogVersion": "arena.hosted-strategy.v1",
                "baseStrategyInstructions": (
                    "Stable numeric strategy: buy grain at or below "
                    "2.100000 and sell excess grain at or above 1.900000."
                ),
                "basePolicyProfile": {
                    "riskBudgetBps": 5000,
                    "minExpectedEdgeBps": 900,
                    "maxInventoryConcentrationBps": 7500,
                    "negotiationConcessionBps": 1200,
                    "explorationBps": 1200,
                },
                "outcome": {
                    "rank": 2,
                    "participantCount": 10,
                    "netWorthAtomic": "21000000",
                    "averageNetWorthAtomic": "20000000",
                    "outcomeScoreBps": 3889,
                },
                "behavior": {
                    "taskCount": 12,
                    "candidateActionCount": 10,
                    "defaultedTaskCount": 2,
                    "rejectedResultCount": 0,
                    "settledTradeCount": 2,
                    "settlementFailureCount": 0,
                    "appliedActionCounts": {
                        "buy": 3,
                        "sell": 2,
                        "pass": 7,
                    },
                    "inputTokens": 4000,
                    "outputTokens": 900,
                    "reasoningTokens": 400,
                },
                "finalPricesAtomic": {
                    "grain": "2000000",
                    "iron": "5000000",
                    "warhorse": "8000000",
                    "gems": "3000000",
                },
                "lastGameMemory": {
                    "schemaVersion": "arena.hosted-game-memory.v1"
                },
            }

        async def complete_learning_job(
            self,
            worker_id: str,
            job: ClaimedLearningJob,
            **values: object,
        ) -> dict[str, object]:
            assert worker_id == "worker-learning"
            assert job.learning_job_id == "learning:test-1"
            self.completion = values
            return {
                "disposition": "activated",
                "strategyRevisionId": "strategy:learned:test",
            }

        async def release_learning_job(
            self,
            worker_id: str,
            job: ClaimedLearningJob,
            **values: object,
        ) -> str:
            del worker_id, job
            self.releases.append(values)
            return "failed"

    class _LearningBuiltModel:
        def __init__(self) -> None:
            self.model = TestModel(
                custom_output_args={
                    "policyProfile": {
                        "riskBudgetBps": 5300,
                        "minExpectedEdgeBps": 1000,
                        "maxInventoryConcentrationBps": 7200,
                        "negotiationConcessionBps": 1100,
                        "explorationBps": 1300,
                    },
                    "lessonSummary": (
                        "Preserve a little more liquidity after this result."
                    ),
                    "adjustments": [
                        "Preserve slightly more cash before the final event.",
                        "Require a modestly clearer concentration edge.",
                    ],
                    "expectedEffect": (
                        "Reduce concentration without abandoning good trades."
                    ),
                    "confidenceBps": 7500,
                }
            )
            self.settings = None
            self.resolved = SimpleNamespace(
                provider_id="official-deepseek",
                model_id="deepseek-v4-flash",
                thinking_enabled=True,
            )
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class _LearningModelFactory:
        def __init__(self) -> None:
            self.built = _LearningBuiltModel()
            self.values: dict[str, object] | None = None

        def build(self, **values: object) -> _LearningBuiltModel:
            self.values = values
            return self.built

    class _IncompleteLearningRepository(_LearningRepository):
        async def load_learning_evidence(
            self,
            worker_id: str,
            learning_job_id: str,
        ) -> dict[str, object]:
            payload = await super().load_learning_evidence(
                worker_id,
                learning_job_id,
            )
            behavior = dict(payload["behavior"])  # type: ignore[arg-type]
            behavior.update(
                {
                    "candidateActionCount": 0,
                    "defaultedTaskCount": 12,
                    "appliedActionCounts": {},
                }
            )
            payload["behavior"] = behavior
            return payload

    class _UnusedLearningModelFactory:
        def build(self, **_: object) -> object:
            raise AssertionError(
                "incomplete evidence must not invoke a model"
            )

    async def scenario() -> None:
        repository = _LearningRepository()
        reader = _Reader()
        model_factory = _LearningModelFactory()
        worker = DurableHostedWorker(
            repository=repository,  # type: ignore[arg-type]
            providers=ProductionProviderBundle(
                registry=CapabilityRegistry(),
                adapters={},
            ),
            secret_reader=reader,
            worker_id="worker-learning",
            model_factory=model_factory,  # type: ignore[arg-type]
        )
        job = ClaimedLearningJob(
            learning_job_id="learning:test-1",
            game_id="game-1",
            game_agent_id="game-agent-1",
            agent_id="agent-1",
            base_strategy_revision_id="strategy:test-1",
            provider="official-deepseek",
            model="deepseek-v4-flash",
            thinking_enabled=True,
            max_output_tokens=16_384,
            secret_ref="arena402/hosted-model/credential-1",
            attempt_count=1,
            lease_expires_at=(
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ),
        )

        await worker._execute_learning(job)

        assert repository.releases == []
        assert repository.completion is not None
        assert repository.completion["gate_passed"] is True
        assert repository.completion["gate_reason"] == "passed"
        assert str(repository.completion["evidence_hash"]).startswith(
            "sha256:"
        )
        assert "public archetype balanced" in str(
            repository.completion["instructions"]
        )
        assert "sell excess grain at or above 1.900000" in str(
            repository.completion["instructions"]
        )
        assert model_factory.built.closed is True
        assert model_factory.values is not None
        assert (
            model_factory.values["requested_max_output_tokens"]
            == 8_192
        )
        assert reader.resolved is not None
        with pytest.raises(SecretStoreOperationError):
            reader.resolved.reveal_for_worker()

        incomplete_repository = _IncompleteLearningRepository()
        unused_reader = _Reader()
        preflight_worker = DurableHostedWorker(
            repository=incomplete_repository,  # type: ignore[arg-type]
            providers=ProductionProviderBundle(
                registry=CapabilityRegistry(),
                adapters={},
            ),
            secret_reader=unused_reader,
            worker_id="worker-learning",
            model_factory=_UnusedLearningModelFactory(),  # type: ignore[arg-type]
        )

        await preflight_worker._execute_learning(job)

        assert incomplete_repository.releases == []
        assert incomplete_repository.completion is not None
        assert incomplete_repository.completion["gate_passed"] is False
        assert (
            incomplete_repository.completion["gate_reason"]
            == "incomplete_verified_evidence"
        )
        assert unused_reader.resolved is None

    asyncio.run(scenario())


def test_learning_repository_loads_foundation_aware_evidence() -> None:
    class _Pool:
        def __init__(self) -> None:
            self.query = ""

        async def fetchval(self, query: str, *args: object) -> object:
            self.query = query
            assert args == ("learning:test-1", "worker-learning")
            return {
                "schemaVersion": "arena.hosted-learning-evidence.v2",
                "baseStrategyInstructions": "Stable strategy foundation.",
            }

    pool = _Pool()
    repository = PostgresHostedWorkerRepository("", pool=pool)

    payload = asyncio.run(
        repository.load_learning_evidence(
            "worker-learning",
            "learning:test-1",
        )
    )

    assert "load_hosted_agent_learning_evidence_v2" in pool.query
    assert payload["baseStrategyInstructions"] == (
        "Stable strategy foundation."
    )
