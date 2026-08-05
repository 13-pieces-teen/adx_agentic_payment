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
    ) == 256
    assert arena_action_output_token_budget(
        configured_tokens=16_384,
        thinking_enabled=True,
    ) == 2_048
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
    assert repository.claim_order == ["tasks", "validations"]


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


def test_worker_executes_pydantic_agent_and_stages_memory_patch() -> None:
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
            return 1

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
        def __init__(self) -> None:
            self.model = TestModel(
                custom_output_args={
                    "action": {"action": "pass"},
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
            self.built = _BuiltModel()
            self.values: dict[str, object] | None = None

        def build(self, **values: object) -> _BuiltModel:
            self.values = values
            return self.built

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

        assert len(repository.attempts) == 1
        assert len(repository.completions) == 1
        assert repository.completions[0].status == "succeeded"
        assert repository.staged[0]["expected_memory_version"] == 4
        assert str(
            repository.staged[0]["runtime_result_id_digest"]
        ).startswith("sha256:")
        assert repository.results[0].status == "succeeded"
        assert repository.results[0].action is not None
        assert repository.results[0].action.action == "pass"
        assert model_factory.values is not None
        assert model_factory.values["api_key"] == "test-provider-key"
        assert model_factory.built.closed is True
        assert reader.resolved is not None
        with pytest.raises(SecretStoreOperationError):
            reader.resolved.reveal_for_worker()

    asyncio.run(scenario())
