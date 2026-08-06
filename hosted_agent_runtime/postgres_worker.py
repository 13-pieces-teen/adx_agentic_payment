"""Durable PostgreSQL Hosted Worker for validation and Arena Agent tasks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from arena_agent_contracts import AgentTaskResultV1, ArenaAgentTaskV1
from arena_core.hashing import sha256_identifier, sha256_text_identifier
from arena_core.public_output_policy import PublicOutputPolicy

from .attempts import AttemptCompletion, AttemptCreated, AttemptRecorder
from .context import HostedArenaAgentContext
from .learning import (
    HostedLearningEvidence,
    HostedStrategyLearningRuntime,
    LearningGateDecision,
    StrategyLearningProposal,
    evaluate_learning_evidence,
    evaluate_learning_proposal,
    render_learned_strategy_instructions,
)
from .memory import HostedGameMemory
from .model_factory import PydanticModelFactory
from .production_providers import ProductionProviderBundle
from .providers import (
    ProviderInvocationError,
    ProviderRequest,
    ProviderUsage,
)
from .runtime import HostedArenaAgentRuntime
from .secret_store import (
    SecretReader,
    SecretReference,
    SecretStoreError,
)
from .strategy import StrategyArchetype


_LOGGER = logging.getLogger(__name__)
_ARENA_VISIBLE_OUTPUT_TOKENS = 8_192
_ARENA_THINKING_OUTPUT_TOKENS = 16_384


def arena_action_output_token_budget(
    *,
    configured_tokens: int,
    thinking_enabled: bool,
) -> int:
    """Clamp Arena actions without silently disabling configured thinking."""

    if configured_tokens <= 0:
        raise ValueError("configured_tokens must be positive")
    ceiling = (
        _ARENA_THINKING_OUTPUT_TOKENS
        if thinking_enabled
        else _ARENA_VISIBLE_OUTPUT_TOKENS
    )
    return min(configured_tokens, ceiling)


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError("worker snapshot must be a JSON object")
    return dict(value)


def _config_value(config: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in config:
            return config[name]
    raise ValueError("worker config is incomplete")


@dataclass(frozen=True, slots=True)
class ClaimedValidation:
    validation_job_id: str
    candidate_config_hash: str
    expected_current_config_hash: str
    deadline_at: datetime
    provider: str
    model: str
    thinking_enabled: bool
    secret_ref: str


@dataclass(frozen=True, slots=True)
class ClaimedTask:
    task: ArenaAgentTaskV1
    deadline_at: datetime
    provider: str
    secret_ref: str
    runtime_config: dict[str, Any]
    attempt_count: int
    recovery_disposition: str
    first_attempt_number: int | None


@dataclass(frozen=True, slots=True)
class ClaimedLearningJob:
    learning_job_id: str
    game_id: str
    game_agent_id: str
    agent_id: str
    base_strategy_revision_id: str
    provider: str
    model: str
    thinking_enabled: bool
    max_output_tokens: int
    secret_ref: str
    attempt_count: int
    lease_expires_at: datetime


class PostgresHostedWorkerRepository:
    def __init__(self, dsn: str, *, pool: object | None = None) -> None:
        if not dsn and pool is None:
            raise ValueError("Hosted Worker PostgreSQL DSN is required")
        self._dsn = dsn
        self._pool = pool
        self._owns_pool = pool is None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        try:
            import asyncpg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("asyncpg is required for Hosted Worker") from exc

        async def initialize_connection(connection: Any) -> None:
            await connection.set_type_codec(
                "jsonb",
                schema="pg_catalog",
                encoder=json.dumps,
                decoder=json.loads,
            )
            await connection.execute("SET ROLE adx_hosted_worker")
            await connection.execute("SET search_path TO pg_catalog, public")

        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=5,
            command_timeout=30,
            init=initialize_connection,
        )

    async def close(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
        self._pool = None

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("Hosted Worker repository is not initialized")
        return self._pool

    async def claim_validations(
        self,
        worker_id: str,
        *,
        limit: int,
        lease_seconds: int,
    ) -> tuple[ClaimedValidation, ...]:
        rows = await self._require_pool().fetch(
            "SELECT * FROM claim_credential_validation_jobs($1, $2, $3)",
            worker_id,
            limit,
            lease_seconds,
        )
        return tuple(
            ClaimedValidation(
                validation_job_id=row["validation_job_id"],
                candidate_config_hash=row["candidate_config_hash"],
                expected_current_config_hash=row[
                    "expected_current_config_hash"
                ],
                deadline_at=row["deadline_at"],
                provider=row["provider"],
                model=row["model"],
                thinking_enabled=row["thinking_enabled"],
                secret_ref=row["secret_ref"],
            )
            for row in rows
        )

    async def start_validation(
        self,
        worker_id: str,
        validation_job_id: str,
    ) -> int:
        return int(
            await self._require_pool().fetchval(
                "SELECT record_credential_validation_attempt($1, $2)",
                validation_job_id,
                worker_id,
            )
        )

    async def complete_validation(
        self,
        worker_id: str,
        job: ClaimedValidation,
        *,
        outcome: str,
        error_class: str | None = None,
        retry_at: datetime | None = None,
    ) -> str:
        return str(
            await self._require_pool().fetchval(
                """
                SELECT complete_credential_validation(
                    $1, $2, $3, $4, $5, $6, $7
                )
                """,
                job.validation_job_id,
                worker_id,
                job.candidate_config_hash,
                job.expected_current_config_hash,
                outcome,
                error_class,
                retry_at,
            )
        )

    async def claim_tasks(
        self,
        worker_id: str,
        *,
        limit: int,
        lease_seconds: int,
    ) -> tuple[ClaimedTask, ...]:
        rows = await self._require_pool().fetch(
            "SELECT * FROM claim_hosted_agent_tasks_v2($1, $2, $3)",
            worker_id,
            limit,
            lease_seconds,
        )
        claimed: list[ClaimedTask] = []
        for row in rows:
            recovery = await self._require_pool().fetchrow(
                "SELECT * FROM prepare_reclaimed_hosted_task($1, $2)",
                row["task_id"],
                worker_id,
            )
            input_snapshot = _json_object(row["input_snapshot"])
            negotiation_id = input_snapshot.get("negotiationId")
            task = ArenaAgentTaskV1.model_validate(
                {
                    "taskId": row["task_id"],
                    "kind": row["task_kind"],
                    "schemaVersion": row["schema_version"],
                    "gameId": row["game_id"],
                    "roundId": row["round_id"],
                    "gameAgentId": row["game_agent_id"],
                    "negotiationId": negotiation_id,
                    "deadlineAt": row["deadline_at"],
                    "idempotencyKey": row["idempotency_key"],
                    "inputHash": row["input_hash"],
                    "input": input_snapshot,
                }
            )
            claimed.append(
                ClaimedTask(
                    task=task,
                    deadline_at=row["deadline_at"],
                    provider=row["provider"],
                    secret_ref=row["secret_ref"],
                    runtime_config=_json_object(
                        row["runtime_config_snapshot"]
                    ),
                    attempt_count=int(row["attempt_count"]),
                    recovery_disposition=recovery["disposition"],
                    first_attempt_number=recovery["next_attempt_no"],
                )
            )
        return tuple(claimed)

    async def submit_result(
        self,
        worker_id: str,
        result: AgentTaskResultV1,
        *,
        message_replaced: bool,
        policy_version: str | None,
        error_class: str | None,
    ) -> str:
        candidate_action = (
            None
            if result.action is None
            else result.action.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
        )
        row = await self._require_pool().fetchrow(
            """
            SELECT * FROM submit_hosted_agent_task_result(
                $1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10
            )
            """,
            worker_id,
            result.task_id,
            sha256_text_identifier(result.result_id),
            sha256_identifier(result),
            result.schema_version,
            result.status,
            # The pool installs a jsonb codec whose encoder owns JSON
            # serialization. Passing an already-serialized string would turn
            # the action into a JSON string, and the Result Sink correctly
            # rejects it because candidate_action must be an object.
            candidate_action,
            message_replaced,
            policy_version,
            error_class,
        )
        return str(row["disposition"])

    async def load_runtime_context(
        self,
        worker_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        value = await self._require_pool().fetchval(
            "SELECT load_hosted_agent_runtime_context($1, $2)",
            task_id,
            worker_id,
        )
        return _json_object(value)

    async def stage_memory_patch(
        self,
        worker_id: str,
        task_id: str,
        *,
        runtime_result_id_digest: str,
        expected_memory_version: int,
        decision_summary: Mapping[str, object],
        memory_patch: Mapping[str, object],
    ) -> bool:
        return bool(
            await self._require_pool().fetchval(
                """
                SELECT stage_hosted_agent_memory_patch(
                    $1, $2, $3, $4, $5::jsonb, $6::jsonb
                )
                """,
                task_id,
                worker_id,
                runtime_result_id_digest,
                expected_memory_version,
                dict(decision_summary),
                dict(memory_patch),
            )
        )

    async def project_memory_patches(self, *, limit: int = 100) -> int:
        return int(
            await self._require_pool().fetchval(
                "SELECT project_hosted_agent_memory_patches($1)",
                limit,
            )
        )

    async def claim_learning_jobs(
        self,
        worker_id: str,
        *,
        limit: int,
        lease_seconds: int,
    ) -> tuple[ClaimedLearningJob, ...]:
        rows = await self._require_pool().fetch(
            """
            SELECT *
            FROM claim_hosted_agent_learning_jobs($1, $2, $3)
            """,
            worker_id,
            limit,
            lease_seconds,
        )
        return tuple(
            ClaimedLearningJob(
                learning_job_id=str(row["learning_job_id"]),
                game_id=str(row["game_id"]),
                game_agent_id=str(row["game_agent_id"]),
                agent_id=str(row["agent_id"]),
                base_strategy_revision_id=str(
                    row["base_strategy_revision_id"]
                ),
                provider=str(row["provider"]),
                model=str(row["model"]),
                thinking_enabled=bool(row["thinking_enabled"]),
                max_output_tokens=int(row["max_output_tokens"]),
                secret_ref=str(row["secret_ref"]),
                attempt_count=int(row["attempt_count"]),
                lease_expires_at=row["lease_expires_at"],
            )
            for row in rows
        )

    async def load_learning_evidence(
        self,
        worker_id: str,
        learning_job_id: str,
    ) -> dict[str, Any]:
        value = await self._require_pool().fetchval(
            "SELECT load_hosted_agent_learning_evidence_v2($1, $2)",
            learning_job_id,
            worker_id,
        )
        return _json_object(value)

    async def complete_learning_job(
        self,
        worker_id: str,
        job: ClaimedLearningJob,
        *,
        evidence_hash: str,
        outcome_score_bps: int,
        source_config_hash: str,
        policy_profile: Mapping[str, object],
        instructions: str,
        proposal: Mapping[str, object],
        gate_summary: Mapping[str, object],
        gate_passed: bool,
        gate_reason: str,
    ) -> dict[str, Any]:
        value = await self._require_pool().fetchval(
            """
            SELECT complete_hosted_agent_learning_job(
                $1, $2, $3, $4, $5, $6::jsonb, $7,
                $8::jsonb, $9::jsonb, $10, $11
            )
            """,
            job.learning_job_id,
            worker_id,
            evidence_hash,
            outcome_score_bps,
            source_config_hash,
            dict(policy_profile),
            instructions,
            dict(proposal),
            dict(gate_summary),
            gate_passed,
            gate_reason,
        )
        return _json_object(value)

    async def release_learning_job(
        self,
        worker_id: str,
        job: ClaimedLearningJob,
        *,
        error_class: str,
        retryable: bool,
    ) -> str:
        return str(
            await self._require_pool().fetchval(
                """
                SELECT release_hosted_agent_learning_job(
                    $1, $2, $3, $4
                )
                """,
                job.learning_job_id,
                worker_id,
                error_class,
                retryable,
            )
        )

    async def start_attempt(
        self,
        worker_id: str,
        attempt: AttemptCreated,
    ) -> int:
        return int(
            await self._require_pool().fetchval(
                """
                SELECT start_agent_task_attempt($1, $2, $3, $4, $5, $6)
                """,
                attempt.task_id,
                worker_id,
                attempt.attempt_id,
                attempt.provider_id,
                attempt.model_id,
                attempt.thinking_enabled,
            )
        )

    async def mark_attempt_sent(
        self,
        worker_id: str,
        attempt_id: str,
    ) -> bool:
        return bool(
            await self._require_pool().fetchval(
                "SELECT mark_agent_task_attempt_request_sent($1, $2)",
                attempt_id,
                worker_id,
            )
        )

    async def finish_attempt(
        self,
        worker_id: str,
        completion: AttemptCompletion,
    ) -> bool:
        usage = completion.usage
        return bool(
            await self._require_pool().fetchval(
                """
                SELECT complete_agent_task_attempt(
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12
                )
                """,
                completion.attempt_id,
                worker_id,
                completion.status,
                completion.latency_ms,
                completion.actual_model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_input_tokens,
                usage.reasoning_tokens,
                usage.complete,
                completion.provider_request_id,
                completion.error_code,
            )
        )

    async def finish_pydantic_attempt(
        self,
        worker_id: str,
        completion: AttemptCompletion,
        *,
        request_count: int,
        tool_call_count: int,
    ) -> bool:
        usage = completion.usage
        return bool(
            await self._require_pool().fetchval(
                """
                SELECT complete_pydantic_agent_task_attempt(
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12, $13, $14
                )
                """,
                completion.attempt_id,
                worker_id,
                completion.status,
                completion.latency_ms,
                completion.actual_model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_input_tokens,
                usage.reasoning_tokens,
                usage.complete,
                completion.provider_request_id,
                completion.error_code,
                request_count,
                tool_call_count,
            )
        )


class PostgresAttemptRecorder(AttemptRecorder):
    def __init__(
        self,
        repository: PostgresHostedWorkerRepository,
        worker_id: str,
    ) -> None:
        self._repository = repository
        self._worker_id = worker_id

    async def create(self, attempt: AttemptCreated) -> None:
        actual = await self._repository.start_attempt(
            self._worker_id,
            attempt,
        )
        if actual != attempt.attempt_number:
            raise RuntimeError("PostgreSQL attempt sequence mismatch")

    async def mark_request_sent(
        self,
        attempt_id: str,
        *,
        request_sent_at: datetime,
    ) -> None:
        del request_sent_at
        if not await self._repository.mark_attempt_sent(
            self._worker_id,
            attempt_id,
        ):
            raise RuntimeError("PostgreSQL attempt send transition failed")

    async def finish(self, completion: AttemptCompletion) -> None:
        if not await self._repository.finish_attempt(
            self._worker_id,
            completion,
        ):
            raise RuntimeError("PostgreSQL attempt completion failed")


class DurableHostedWorker:
    def __init__(
        self,
        *,
        repository: PostgresHostedWorkerRepository,
        providers: ProductionProviderBundle,
        secret_reader: SecretReader,
        worker_id: str | None = None,
        lease_seconds: int = 600,
        task_concurrency: int = 5,
        model_factory: PydanticModelFactory | None = None,
    ) -> None:
        if task_concurrency < 1 or task_concurrency > 32:
            raise ValueError("task_concurrency must be between 1 and 32")
        self._repository = repository
        self._providers = providers
        self._secret_reader = secret_reader
        self._worker_id = worker_id or f"hosted-worker-{uuid.uuid4().hex[:16]}"
        self._lease_seconds = lease_seconds
        self._task_concurrency = task_concurrency
        self._model_factory = model_factory
        self._stopping = asyncio.Event()
        self._public_policy = PublicOutputPolicy()

    async def run_once(self) -> int:
        # Arena actions have deadlines and therefore receive priority over
        # credential validation. A create burst cannot consume the whole cycle.
        processed = 0
        tasks = await self._repository.claim_tasks(
            self._worker_id,
            limit=self._task_concurrency,
            lease_seconds=self._lease_seconds,
        )
        await asyncio.gather(
            *(self._execute_task_safely(task) for task in tasks)
        )
        processed += len(tasks)

        validations = await self._repository.claim_validations(
            self._worker_id,
            limit=2,
            lease_seconds=self._lease_seconds,
        )
        for job in validations:
            try:
                await self._validate(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.error(
                    "hosted_validation_processing_failed",
                    extra={"validation_job_id": job.validation_job_id},
                )
            processed += 1
        processed += await self._repository.project_memory_patches(limit=100)
        learning_jobs = await self._repository.claim_learning_jobs(
            self._worker_id,
            limit=1,
            lease_seconds=self._lease_seconds,
        )
        for learning_job in learning_jobs:
            await self._execute_learning_safely(learning_job)
            processed += 1
        return processed

    async def _execute_task_safely(self, task: ClaimedTask) -> None:
        try:
            await self._execute_task(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The lease is the durable recovery boundary. Never log a raw
            # SDK/database exception because it may contain request data.
            _LOGGER.error(
                "hosted_task_processing_failed_%s",
                type(exc).__name__,
                extra={"task_id": task.task.task_id},
            )

    async def _execute_learning_safely(
        self,
        job: ClaimedLearningJob,
    ) -> None:
        try:
            await self._execute_learning(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.error(
                "hosted_learning_processing_failed_%s",
                type(exc).__name__,
                extra={"learning_job_id": job.learning_job_id},
            )
            try:
                await self._repository.release_learning_job(
                    self._worker_id,
                    job,
                    error_class="internal_learning_failure",
                    retryable=True,
                )
            except Exception:
                _LOGGER.error(
                    "hosted_learning_release_failed",
                    extra={"learning_job_id": job.learning_job_id},
                )

    async def run_forever(self, poll_seconds: float = 1.0) -> None:
        while not self._stopping.is_set():
            try:
                count = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A temporary PostgreSQL outage must not terminate the durable
                # process. Claimed work is recovered by its lease.
                _LOGGER.error("hosted_worker_cycle_failed")
                count = 0
            if count == 0:
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=poll_seconds,
                    )
                except TimeoutError:
                    pass

    def stop(self) -> None:
        self._stopping.set()

    async def _validate(self, job: ClaimedValidation) -> None:
        await self._repository.start_validation(
            self._worker_id,
            job.validation_job_id,
        )
        adapter = self._providers.adapters.get(job.provider)
        if adapter is None:
            await self._repository.complete_validation(
                self._worker_id,
                job,
                outcome="permanent_failure",
                error_class="adapter_mismatch",
            )
            return
        remaining_ms = max(
            1,
            min(
                30_000,
                int(
                    (job.deadline_at - datetime.now(timezone.utc))
                    .total_seconds()
                    * 1000
                ),
            ),
        )
        secret = None
        try:
            secret = await asyncio.wait_for(
                self._secret_reader.resolve_for_worker(
                    SecretReference(job.secret_ref)
                ),
                timeout=remaining_ms / 1000,
            )
            response = await asyncio.wait_for(
                adapter.invoke(
                    ProviderRequest(
                        attempt_id=f"validation-{job.validation_job_id}",
                        task_id=f"validation-{job.validation_job_id}",
                        task_kind="arena.decide",
                        idempotency_key=job.validation_job_id,
                        model_id=job.model,
                        prompt_version="arena.credential-validation.v1",
                        context_version="arena.credential-validation.v1",
                        output_version="arena.credential-validation.v1",
                        system_instructions=(
                            "This is a credential and model availability check. "
                            "Return exactly this JSON object and nothing else: "
                            '{"ok":true}'
                        ),
                        input_json='{"credentialValidation":true}',
                        output_schema_json=(
                            '{"type":"object","properties":'
                            '{"ok":{"type":"boolean"}},'
                            '"required":["ok"],"additionalProperties":false}'
                        ),
                        # Validation checks authentication, model access, and
                        # minimal JSON output. It must not inherit the Agent's
                        # reasoning mode: a small validation response can
                        # otherwise spend its whole budget on reasoning and
                        # falsely mark a valid credential as invalid.
                        thinking_enabled=False,
                        thinking_parameter_name=None,
                        max_output_tokens=128,
                        request_timeout_ms=remaining_ms,
                    ),
                    secret,
                ),
                timeout=remaining_ms / 1000,
            )
            if response.structured_output.get("ok") is not True:
                raise ProviderInvocationError("invalid_structured_output")
        except (TimeoutError, SecretStoreError):
            await self._repository.complete_validation(
                self._worker_id,
                job,
                outcome="transient_failure",
                error_class="provider_unavailable",
                retry_at=datetime.now(timezone.utc) + timedelta(seconds=5),
            )
            return
        except ProviderInvocationError as exc:
            # A malformed availability-probe response does not prove that the
            # credential or model is invalid. DeepSeek can occasionally spend
            # the small validation budget without emitting the exact JSON
            # object, so let the durable job use its existing bounded retry.
            # Authentication and other deterministic request failures remain
            # permanent.
            transient = (
                exc.retryable
                or exc.outcome_unknown
                or exc.code == "invalid_structured_output"
            )
            await self._repository.complete_validation(
                self._worker_id,
                job,
                outcome=(
                    "transient_failure"
                    if transient
                    else "permanent_failure"
                ),
                error_class=exc.code,
                retry_at=(
                    datetime.now(timezone.utc) + timedelta(seconds=5)
                    if transient
                    else None
                ),
            )
            return
        except Exception:
            # Credential validation has no business side effect. Unknown
            # adapter/backend failures can be retried within the job budget.
            await self._repository.complete_validation(
                self._worker_id,
                job,
                outcome="transient_failure",
                error_class="provider_unavailable",
                retry_at=datetime.now(timezone.utc) + timedelta(seconds=5),
            )
            return
        finally:
            if secret is not None:
                secret.close()
        await self._repository.complete_validation(
            self._worker_id,
            job,
            outcome="succeeded",
        )

    async def _execute_task(self, claimed: ClaimedTask) -> None:
        if claimed.recovery_disposition != "execute":
            digest = sha256_text_identifier(
                f"hosted-recovery:{claimed.task.task_id}"
            ).split(":", 1)[1][:40]
            result = AgentTaskResultV1(
                result_id=f"hosted-recovery-{digest}",
                task_id=claimed.task.task_id,
                schema_version="arena.agent-result.v1",
                status="failed",
            )
            await self._repository.submit_result(
                self._worker_id,
                result,
                message_replaced=False,
                policy_version=None,
                error_class=(
                    "request_outcome_unknown"
                    if claimed.recovery_disposition == "terminal_unknown"
                    else "attempts_exhausted"
                ),
            )
            return

        if self._model_factory is None:
            await self._submit_runtime_failure(
                claimed,
                status="failed",
                error_class="adapter_mismatch",
            )
            return
        await self._execute_pydantic_task(claimed)

    async def _execute_pydantic_task(self, claimed: ClaimedTask) -> None:
        if self._model_factory is None:
            raise RuntimeError("PydanticAI model factory is unavailable")

        context_payload = await self._repository.load_runtime_context(
            self._worker_id,
            claimed.task.task_id,
        )
        memory_state = _json_object(
            context_payload.get("memoryState", {})
        )
        context = HostedArenaAgentContext(
            task=claimed.task,
            agent_id=str(context_payload["agentId"]),
            strategy_revision_id=str(
                context_payload["strategyRevisionId"]
            ),
            strategy_revision_no=int(
                context_payload["strategyRevisionNo"]
            ),
            strategy_archetype=StrategyArchetype(
                str(context_payload["strategyArchetype"])
            ),
            strategy_catalog_version=str(
                context_payload["strategyCatalogVersion"]
            ),
            strategy_instructions=str(
                context_payload["strategyInstructions"]
            ),
            game_memory=HostedGameMemory(
                memory_version=int(context_payload["memoryVersion"]),
                state=memory_state,
            ),
        )
        config = claimed.runtime_config
        model_id = str(
            _config_value(config, "model_id", "modelId", "model")
        )
        thinking_enabled = bool(
            _config_value(
                config,
                "thinking_enabled",
                "thinkingEnabled",
            )
        )
        remaining_ms = max(
            0,
            int(
                (
                    min(claimed.deadline_at, claimed.task.deadline_at)
                    - datetime.now(timezone.utc)
                ).total_seconds()
                * 1000
            ),
        )
        if remaining_ms <= 0:
            await self._submit_runtime_failure(
                claimed,
                status="timed_out",
                error_class="deadline_exceeded",
            )
            return

        secret = None
        built_model = None
        try:
            secret = await asyncio.wait_for(
                self._secret_reader.resolve_for_worker(
                    SecretReference(claimed.secret_ref)
                ),
                timeout=remaining_ms / 1000,
            )
            api_key = secret.reveal_for_worker()
            built_model = self._model_factory.build(
                provider_id=claimed.provider,
                model_id=model_id,
                api_key=api_key,
                thinking_enabled=thinking_enabled,
                remaining_timeout_ms=remaining_ms,
                requested_max_output_tokens=(
                    arena_action_output_token_budget(
                        configured_tokens=int(
                            _config_value(
                                config,
                                "max_output_tokens",
                                "maxOutputTokens",
                            )
                        ),
                        thinking_enabled=thinking_enabled,
                    )
                ),
            )
            api_key = ""
        except TimeoutError:
            await self._submit_runtime_failure(
                claimed,
                status="timed_out",
                error_class="deadline_exceeded",
            )
            return
        except SecretStoreError:
            await self._submit_runtime_failure(
                claimed,
                status="failed",
                error_class="provider_unavailable",
            )
            return
        except (ValueError, KeyError):
            await self._submit_runtime_failure(
                claimed,
                status="failed",
                error_class="adapter_mismatch",
            )
            return
        finally:
            if secret is not None:
                secret.close()

        attempt_number = (
            claimed.first_attempt_number
            if claimed.first_attempt_number is not None
            else claimed.attempt_count + 1
        )
        attempt_id = self._pydantic_attempt_id(
            claimed.task,
            attempt_number,
        )
        recorder = PostgresAttemptRecorder(
            self._repository,
            self._worker_id,
        )
        try:
            await recorder.create(
                AttemptCreated(
                    attempt_id=attempt_id,
                    task_id=claimed.task.task_id,
                    attempt_number=attempt_number,
                    provider_id=built_model.resolved.provider_id,
                    model_id=built_model.resolved.model_id,
                    thinking_enabled=(
                        built_model.resolved.thinking_enabled
                    ),
                    created_at=datetime.now(timezone.utc),
                )
            )
            await recorder.mark_request_sent(
                attempt_id,
                request_sent_at=datetime.now(timezone.utc),
            )
            execution = await HostedArenaAgentRuntime(
                model=built_model.model,
                context=context,
                model_settings=built_model.settings,
                actual_model=built_model.resolved.model_id,
            ).execute_with_metadata(
                claimed.task,
                claimed.deadline_at,
            )
            attempt_status = (
                "succeeded"
                if execution.result.status == "succeeded"
                else (
                    "unknown"
                    if execution.error_code == "request_outcome_unknown"
                    else "failed"
                )
            )
            attempt_error = execution.error_code
            if attempt_status != "succeeded" and attempt_error is None:
                attempt_error = (
                    "deadline_exceeded"
                    if execution.result.status == "timed_out"
                    else "permanent_request"
                )
            completion = AttemptCompletion(
                attempt_id=attempt_id,
                status=attempt_status,
                finished_at=datetime.now(timezone.utc),
                latency_ms=execution.latency_ms,
                usage=execution.usage,
                actual_model=execution.actual_model,
                error_code=attempt_error,
            )
            if not await self._repository.finish_pydantic_attempt(
                self._worker_id,
                completion,
                request_count=execution.request_count,
                tool_call_count=execution.tool_call_count,
            ):
                raise RuntimeError(
                    "PostgreSQL Pydantic attempt completion failed"
                )
        finally:
            try:
                await built_model.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.error(
                    "hosted_model_close_failed_%s",
                    type(exc).__name__,
                    extra={"task_id": claimed.task.task_id},
                )

        retry_remaining_ms = max(
            0,
            int(
                (
                    min(claimed.deadline_at, claimed.task.deadline_at)
                    - datetime.now(timezone.utc)
                ).total_seconds()
                * 1000
            ),
        )
        if (
            execution.error_code == "invalid_structured_output"
            and attempt_number < 2
            and retry_remaining_ms >= 5_000
        ):
            await self._execute_pydantic_task(
                replace(
                    claimed,
                    attempt_count=attempt_number,
                    recovery_disposition="execute",
                    first_attempt_number=attempt_number + 1,
                )
            )
            return

        result, replaced, policy_version = self._sanitize_result(
            claimed,
            execution.result,
        )
        if execution.agent_output is not None:
            try:
                await self._repository.stage_memory_patch(
                    self._worker_id,
                    claimed.task.task_id,
                    runtime_result_id_digest=sha256_text_identifier(
                        result.result_id
                    ),
                    expected_memory_version=(
                        context.game_memory.memory_version
                    ),
                    decision_summary=(
                        execution.agent_output.decision_summary.model_dump(
                            mode="json"
                        )
                    ),
                    memory_patch=(
                        execution.agent_output.memory_patch.model_dump(
                            mode="json"
                        )
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Learning is subordinate to the authoritative action. A
                # projection outage must not turn a valid candidate into an
                # unknown business outcome.
                _LOGGER.error(
                    "hosted_memory_patch_stage_failed_%s",
                    type(exc).__name__,
                    extra={"task_id": claimed.task.task_id},
                )
        await self._repository.submit_result(
            self._worker_id,
            result,
            message_replaced=replaced,
            policy_version=policy_version,
            error_class=(
                None
                if result.status == "succeeded"
                else (
                    execution.error_code
                    or "runtime_failed"
                )
            ),
        )

    async def _execute_learning(self, job: ClaimedLearningJob) -> None:
        if self._model_factory is None:
            await self._repository.release_learning_job(
                self._worker_id,
                job,
                error_class="adapter_mismatch",
                retryable=False,
            )
            return

        try:
            evidence = HostedLearningEvidence.model_validate(
                await self._repository.load_learning_evidence(
                    self._worker_id,
                    job.learning_job_id,
                )
            )
        except (ValueError, KeyError):
            await self._repository.release_learning_job(
                self._worker_id,
                job,
                error_class="invalid_learning_evidence",
                retryable=False,
            )
            return

        evidence_decision = evaluate_learning_evidence(evidence)
        if not evidence_decision.passed:
            proposal = StrategyLearningProposal(
                policy_profile=evidence.base_policy_profile,
                lesson_summary=(
                    "No strategy candidate was generated because verified "
                    "action evidence was incomplete."
                ),
                adjustments=[
                    "Keep the current bounded policy until complete applied "
                    "action evidence is available."
                ],
                expected_effect=(
                    "Avoid learning from defaulted or incomplete runtime "
                    "evidence."
                ),
                confidence_bps=0,
            )
            await self._complete_learning_proposal(
                job,
                evidence,
                proposal,
                evaluate_learning_proposal(evidence, proposal),
            )
            return

        remaining_ms = max(
            0,
            min(
                180_000,
                int(
                    (
                        job.lease_expires_at
                        - datetime.now(timezone.utc)
                    ).total_seconds()
                    * 1000
                ),
            ),
        )
        if remaining_ms <= 0:
            await self._repository.release_learning_job(
                self._worker_id,
                job,
                error_class="deadline_exceeded",
                retryable=True,
            )
            return

        secret = None
        built_model = None
        try:
            secret = await asyncio.wait_for(
                self._secret_reader.resolve_for_worker(
                    SecretReference(job.secret_ref)
                ),
                timeout=remaining_ms / 1000,
            )
            api_key = secret.reveal_for_worker()
            built_model = self._model_factory.build(
                provider_id=job.provider,
                model_id=job.model,
                api_key=api_key,
                thinking_enabled=job.thinking_enabled,
                remaining_timeout_ms=remaining_ms,
                requested_max_output_tokens=min(
                    job.max_output_tokens,
                    8_192,
                ),
            )
            api_key = ""
        except TimeoutError:
            await self._repository.release_learning_job(
                self._worker_id,
                job,
                error_class="deadline_exceeded",
                retryable=True,
            )
            return
        except SecretStoreError:
            await self._repository.release_learning_job(
                self._worker_id,
                job,
                error_class="provider_unavailable",
                retryable=True,
            )
            return
        except (ValueError, KeyError):
            await self._repository.release_learning_job(
                self._worker_id,
                job,
                error_class="adapter_mismatch",
                retryable=False,
            )
            return
        finally:
            if secret is not None:
                secret.close()

        try:
            execution = await HostedStrategyLearningRuntime(
                model=built_model.model,
                model_settings=built_model.settings,
                actual_model=built_model.resolved.model_id,
            ).execute(
                evidence,
                timeout_seconds=remaining_ms / 1000,
            )
        finally:
            try:
                await built_model.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.error(
                    "hosted_learning_model_close_failed_%s",
                    type(exc).__name__,
                    extra={"learning_job_id": job.learning_job_id},
                )

        if execution.proposal is None:
            error_class = execution.error_code or "runtime_failed"
            await self._repository.release_learning_job(
                self._worker_id,
                job,
                error_class=error_class,
                retryable=error_class
                in {
                    "invalid_structured_output",
                    "rate_limited",
                    "provider_unavailable",
                    "request_outcome_unknown",
                },
            )
            return

        proposal = execution.proposal
        gate = evaluate_learning_proposal(evidence, proposal)
        await self._complete_learning_proposal(
            job,
            evidence,
            proposal,
            gate,
        )

    async def _complete_learning_proposal(
        self,
        job: ClaimedLearningJob,
        evidence: HostedLearningEvidence,
        proposal: StrategyLearningProposal,
        gate: LearningGateDecision,
    ) -> None:
        instructions = render_learned_strategy_instructions(
            archetype=evidence.archetype,
            base_strategy_instructions=(
                evidence.base_strategy_instructions
            ),
            profile=proposal.policy_profile,
            adjustments=proposal.adjustments,
        )
        evidence_payload = evidence.model_dump(
            mode="json",
            by_alias=True,
        )
        proposal_payload = proposal.model_dump(
            mode="json",
            by_alias=True,
        )
        evidence_hash = sha256_identifier(evidence_payload)
        source_config_hash = sha256_identifier(
            {
                "agentId": evidence.agent_id,
                "baseStrategyRevisionId": (
                    evidence.base_strategy_revision_id
                ),
                "learningJobId": evidence.learning_job_id,
                "evidenceHash": evidence_hash,
                "policyProfile": proposal_payload["policyProfile"],
                "instructions": instructions,
            }
        )
        await self._repository.complete_learning_job(
            self._worker_id,
            job,
            evidence_hash=evidence_hash,
            outcome_score_bps=evidence.outcome.outcome_score_bps,
            source_config_hash=source_config_hash,
            policy_profile=proposal.policy_profile.model_dump(
                mode="json",
                by_alias=True,
            ),
            instructions=instructions,
            proposal=proposal_payload,
            gate_summary=gate.evidence_summary,
            gate_passed=gate.passed,
            gate_reason=gate.reason,
        )

    async def _submit_runtime_failure(
        self,
        claimed: ClaimedTask,
        *,
        status: str,
        error_class: str,
    ) -> None:
        digest = sha256_text_identifier(
            f"pydantic-hosted-failed:{claimed.task.task_id}:{status}"
        ).split(":", 1)[1][:40]
        result = AgentTaskResultV1(
            result_id=f"pydantic-hosted-failed-{digest}",
            task_id=claimed.task.task_id,
            schema_version="arena.agent-result.v1",
            status=status,
        )
        await self._repository.submit_result(
            self._worker_id,
            result,
            message_replaced=False,
            policy_version=None,
            error_class=error_class,
        )

    @staticmethod
    def _pydantic_attempt_id(
        task: ArenaAgentTaskV1,
        attempt_number: int,
    ) -> str:
        digest = hashlib.sha256(
            (
                "arena402:pydantic-hosted-attempt:v1\0"
                f"{task.task_id}\0{task.input_hash}"
            ).encode("utf-8")
        ).hexdigest()
        return f"pydantic-hosted-attempt-{digest[:32]}-{attempt_number}"

    def _sanitize_result(
        self,
        claimed: ClaimedTask,
        result: AgentTaskResultV1,
    ) -> tuple[AgentTaskResultV1, bool, str | None]:
        action = result.action
        message = getattr(action, "message", None)
        if action is None or message is None:
            return result, False, None
        decision = self._public_policy.sanitize(
            message=message,
            action=action.action,
            price=getattr(action, "price", None),
            role=getattr(claimed.task.input, "role", None),
            strategy_instructions=str(
                claimed.runtime_config.get("strategy_instructions")
                or claimed.runtime_config.get("strategyInstructions")
                or ""
            ),
        )
        sanitized = action.model_copy(update={"message": decision.message})
        return (
            result.model_copy(update={"action": sanitized}),
            decision.message_replaced,
            decision.policy_version,
        )

__all__ = [
    "ClaimedTask",
    "ClaimedValidation",
    "DurableHostedWorker",
    "PostgresAttemptRecorder",
    "PostgresHostedWorkerRepository",
    "arena_action_output_token_budget",
]
