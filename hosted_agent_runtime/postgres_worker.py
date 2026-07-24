"""Durable PostgreSQL Hosted Worker for validation and Arena Agent tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from arena_agent_contracts import AgentTaskResultV1, ArenaAgentTaskV1
from arena_core.hashing import sha256_identifier, sha256_text_identifier
from arena_core.public_output_policy import PublicOutputPolicy

from .direct_model_driver import (
    AttemptCompletion,
    AttemptCreated,
    AttemptRecorder,
    DirectModelConfig,
    DirectModelDriver,
    DirectModelInfrastructureError,
)
from .production_providers import ProductionProviderBundle
from .providers import (
    ProviderInvocationError,
    ProviderRequest,
    ProviderUsage,
)
from .secret_store import (
    SecretReader,
    SecretReference,
    SecretStoreError,
)


_LOGGER = logging.getLogger(__name__)


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
    ) -> None:
        self._repository = repository
        self._providers = providers
        self._secret_reader = secret_reader
        self._worker_id = worker_id or f"hosted-worker-{uuid.uuid4().hex[:16]}"
        self._lease_seconds = lease_seconds
        self._stopping = asyncio.Event()
        self._public_policy = PublicOutputPolicy()

    async def run_once(self) -> int:
        # Arena actions have deadlines and therefore receive priority over
        # credential validation. A create burst cannot consume the whole cycle.
        processed = 0
        tasks = await self._repository.claim_tasks(
            self._worker_id,
            limit=5,
            lease_seconds=self._lease_seconds,
        )
        for task in tasks:
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
            processed += 1

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
        return processed

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
            transient = exc.retryable or exc.outcome_unknown
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
        config = claimed.runtime_config
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
        adapter = self._providers.adapters.get(claimed.provider)
        if adapter is None:
            digest = sha256_text_identifier(
                f"hosted-adapter-failed:{claimed.task.task_id}"
            ).split(":", 1)[1][:40]
            result = AgentTaskResultV1(
                result_id=f"hosted-failed-{digest}",
                task_id=claimed.task.task_id,
                schema_version="arena.agent-result.v1",
                status="failed",
            )
            await self._repository.submit_result(
                self._worker_id,
                result,
                message_replaced=False,
                policy_version=None,
                error_class="adapter_mismatch",
            )
            return

        driver = DirectModelDriver(
            registry=self._providers.registry,
            provider=adapter,
            secret_reader=self._secret_reader,
            config=DirectModelConfig(
                provider_id=claimed.provider,
                model_id=str(
                    _config_value(config, "model_id", "modelId", "model")
                ),
                credential_ref=SecretReference(claimed.secret_ref),
                thinking_enabled=bool(
                    _config_value(
                        config,
                        "thinking_enabled",
                        "thinkingEnabled",
                    )
                ),
                strategy_instructions=str(
                    _config_value(
                        config,
                        "strategy_instructions",
                        "strategyInstructions",
                    )
                ),
                requested_max_output_tokens=int(
                    _config_value(
                        config,
                        "max_output_tokens",
                        "maxOutputTokens",
                    )
                ),
            ),
            attempt_recorder=PostgresAttemptRecorder(
                self._repository,
                self._worker_id,
            ),
        )
        try:
            result = await driver.execute(
                claimed.task,
                claimed.deadline_at,
                first_attempt_number=(
                    claimed.first_attempt_number
                    if claimed.first_attempt_number is not None
                    else claimed.attempt_count + 1
                ),
            )
        except DirectModelInfrastructureError:
            return

        result, replaced, policy_version = self._sanitize_result(
            claimed,
            result,
        )
        await self._repository.submit_result(
            self._worker_id,
            result,
            message_replaced=replaced,
            policy_version=policy_version,
            error_class=(
                None if result.status == "succeeded" else "runtime_failed"
            ),
        )

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
]
