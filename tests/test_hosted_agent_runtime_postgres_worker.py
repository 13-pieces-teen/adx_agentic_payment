from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from hosted_agent_runtime.capabilities import CapabilityRegistry
from hosted_agent_runtime.postgres_worker import (
    ClaimedValidation,
    DurableHostedWorker,
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
