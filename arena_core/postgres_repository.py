"""PostgreSQL implementation of the Arena Core repository contract.

All terminal timestamps and state transitions are owned by PostgreSQL
functions backed by ``clock_timestamp()``. Runtime-controlled result ids are
validated and reduced to a SHA-256 digest before any SQL parameter is built.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Final

from arena_agent_contracts import (
    AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
    AgentTaskResultV1,
    ArenaAgentTaskV1,
    ArenaNegotiateInputV1,
)

from .hashing import (
    canonical_json_bytes,
    sha256_identifier,
    sha256_text_identifier,
)
from .ingress_security import (
    secure_config_snapshot,
    validate_runtime_result_identifiers,
)
from .models import (
    AppliedArenaAction,
    ArenaResultRecord,
    ArenaTaskRecord,
    ConnectorTaskClaim,
    ResultApplyStatus,
    ResultSubmissionReceipt,
    SubmissionDisposition,
    TaskEventRecord,
    TaskStatus,
)
from .repository import (
    ArenaIdempotencyConflictError,
    ArenaRepositoryError,
    ArenaResultConflictError,
    ArenaTaskNotFoundError,
)


_ARENA_CORE_ROLE: Final[str] = "adx_arena_core"

_TASK_COLUMNS: Final[str] = """
    task_id,
    task_kind,
    schema_version,
    game_id,
    round_id,
    game_agent_id,
    negotiation_id,
    deadline_at,
    idempotency_key,
    input_snapshot,
    input_hash,
    runtime_config_snapshot,
    config_hash,
    status,
    attempt_count,
    leased_by,
    lease_expires_at,
    created_at,
    completed_at
"""

_RESULT_COLUMNS: Final[str] = """
    result_id,
    task_id,
    result_schema_version,
    result_hash,
    runtime_status,
    candidate_action,
    message_replaced,
    public_output_policy_version,
    result_received_at,
    apply_status,
    arena_applied_at,
    arena_rejected_at,
    error_class
"""

_APPLIED_COLUMNS: Final[str] = """
    task_id,
    result_id,
    task_kind,
    application_outcome,
    applied_action,
    authoritative_entered_at,
    applied_at
"""


def _mapping(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return dict(row)


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _json_object(value: Any) -> dict[str, Any]:
    resolved = _json_value(value)
    if not isinstance(resolved, Mapping):
        raise ArenaRepositoryError("PostgreSQL returned a non-object JSON value")
    return dict(resolved)


def _json_parameter(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _aware_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ArenaRepositoryError(f"PostgreSQL returned an invalid {field}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ArenaRepositoryError(f"PostgreSQL returned a naive {field}")
    return value


def _optional_timestamp(value: Any, *, field: str) -> datetime | None:
    if value is None:
        return None
    return _aware_timestamp(value, field=field)


def _task_record(row: Any) -> ArenaTaskRecord:
    data = _mapping(row)
    input_snapshot = secure_config_snapshot(
        _json_object(data["input_snapshot"])
    )
    config_snapshot = secure_config_snapshot(
        _json_object(data["runtime_config_snapshot"])
    )
    task = ArenaAgentTaskV1.model_validate(
        {
            "taskId": data["task_id"],
            "kind": data["task_kind"],
            "schemaVersion": data["schema_version"],
            "gameId": data["game_id"],
            "roundId": data["round_id"],
            "gameAgentId": data["game_agent_id"],
            "negotiationId": data["negotiation_id"],
            "deadlineAt": data["deadline_at"],
            "idempotencyKey": data["idempotency_key"],
            "inputHash": data["input_hash"],
            "input": input_snapshot,
        }
    )
    if sha256_identifier(task.input) != task.input_hash:
        raise ArenaIdempotencyConflictError(
            "Persisted Arena task input hash does not match its snapshot"
        )
    if sha256_identifier(config_snapshot) != data["config_hash"]:
        raise ArenaIdempotencyConflictError(
            "Persisted Arena config hash does not match its snapshot"
        )
    return ArenaTaskRecord(
        task=task,
        config_snapshot=config_snapshot,
        config_hash=data["config_hash"],
        status=TaskStatus(data["status"]),
        created_at=_aware_timestamp(data["created_at"], field="task created_at"),
        attempt_count=int(data["attempt_count"]),
        leased_by=data["leased_by"],
        lease_expires_at=_optional_timestamp(
            data["lease_expires_at"], field="task lease_expires_at"
        ),
        completed_at=_optional_timestamp(
            data["completed_at"], field="task completed_at"
        ),
    )


def _result_record(row: Any) -> ArenaResultRecord:
    data = _mapping(row)
    candidate_action = (
        None
        if data["candidate_action"] is None
        else _json_object(data["candidate_action"])
    )
    result = AgentTaskResultV1.model_validate(
        {
            "resultId": data["result_id"],
            "taskId": data["task_id"],
            "schemaVersion": data["result_schema_version"],
            "status": data["runtime_status"],
            "action": candidate_action,
        }
    )
    return ArenaResultRecord(
        result=result,
        result_received_at=_aware_timestamp(
            data["result_received_at"], field="result_received_at"
        ),
        result_hash=data["result_hash"],
        apply_status=ResultApplyStatus(data["apply_status"]),
        message_replaced=bool(data["message_replaced"]),
        public_output_policy_version=data["public_output_policy_version"],
        safe_error_class=data["error_class"],
        arena_applied_at=_optional_timestamp(
            data["arena_applied_at"], field="arena_applied_at"
        ),
        arena_rejected_at=_optional_timestamp(
            data["arena_rejected_at"], field="arena_rejected_at"
        ),
        rejection_reason=(
            data["error_class"]
            if data["apply_status"] == ResultApplyStatus.REJECTED.value
            else None
        ),
    )


def _applied_action(row: Any) -> AppliedArenaAction:
    data = _mapping(row)
    action = (
        None
        if data["applied_action"] is None
        else _json_object(data["applied_action"])
    )
    return AppliedArenaAction(
        task_id=data["task_id"],
        result_id=data["result_id"],
        kind=data["task_kind"],
        outcome=data["application_outcome"],
        action=action,
        entered_at=_aware_timestamp(
            data["authoritative_entered_at"],
            field="authoritative_entered_at",
        ),
        applied_at=_aware_timestamp(data["applied_at"], field="applied_at"),
    )


class PostgresArenaCoreRepository:
    """Arena Core persistence using migration 003 tables and functions."""

    def __init__(self, database_url: str, *, pool: Any | None = None) -> None:
        self.database_url = database_url
        self._pool: Any = pool
        self._owns_pool = pool is None
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        async with self._initialize_lock:
            if self._pool is not None:
                return
            try:
                import asyncpg  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - production dependency
                raise RuntimeError(
                    "asyncpg is required for PostgreSQL Arena persistence"
                ) from exc
            self._pool = await asyncpg.create_pool(
                self.database_url,
                min_size=1,
                max_size=5,
                command_timeout=30,
                init=self._initialize_connection,
                setup=self._setup_connection,
            )
            self._owns_pool = True

    async def close(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
            self._pool = None

    @staticmethod
    async def _initialize_connection(connection: Any) -> None:
        await connection.set_type_codec(
            "json",
            schema="pg_catalog",
            encoder=_json_parameter,
            decoder=json.loads,
        )
        await connection.set_type_codec(
            "jsonb",
            schema="pg_catalog",
            encoder=_json_parameter,
            decoder=json.loads,
        )

    @staticmethod
    async def _setup_connection(connection: Any) -> None:
        await connection.execute(f"SET ROLE {_ARENA_CORE_ROLE}")
        await connection.execute("SET search_path TO pg_catalog, public")

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("Arena PostgreSQL repository is not initialized")
        return self._pool

    async def create_task(
        self,
        *,
        task: ArenaAgentTaskV1,
        config_snapshot: dict,
        config_hash: str,
        created_at: datetime,
    ) -> ArenaTaskRecord:
        del created_at  # PostgreSQL clock_timestamp() is authoritative.
        task_snapshot = task.model_copy(deep=True)
        secure_config_snapshot(
            task_snapshot.input.model_dump(
                mode="python",
                by_alias=True,
                exclude_none=False,
            )
        )
        secured_config = secure_config_snapshot(config_snapshot)
        if sha256_identifier(task_snapshot.input) != task_snapshot.input_hash:
            raise ArenaIdempotencyConflictError(
                "Arena task input hash does not match its snapshot"
            )
        if sha256_identifier(secured_config) != config_hash:
            raise ArenaIdempotencyConflictError(
                "Arena task config hash does not match its snapshot"
            )

        default_result = self._default_result(task_snapshot.task_id)
        default_result_id = default_result.result_id
        default_result_hash = sha256_identifier(default_result)
        input_parameter = task_snapshot.input.model_dump(
            mode="json", by_alias=True, exclude_none=False
        )
        config_parameter = secured_config
        turn_sequence = (
            task_snapshot.input.turn_sequence
            if isinstance(task_snapshot.input, ArenaNegotiateInputV1)
            else None
        )

        pool = self._require_pool()
        try:
            async with pool.acquire() as connection:
                async with connection.transaction():
                    frozen = await connection.fetchrow(
                        """
                        SELECT
                            ga.runtime_binding_id,
                            ga.config_snapshot,
                            ga.config_hash,
                            b.runtime_kind,
                            hc.credential_id
                        FROM game_agents AS ga
                        JOIN arena_runtime_bindings AS b
                          ON b.runtime_binding_id = ga.runtime_binding_id
                         AND b.agent_id = ga.agent_id
                        LEFT JOIN arena_hosted_configs AS hc
                          ON hc.hosted_config_id = b.hosted_config_id
                         AND hc.agent_id = ga.agent_id
                        WHERE ga.game_agent_id = $1
                          AND ga.game_id = $2
                          AND ga.status IN ('joined', 'active', 'settling')
                        """,
                        task_snapshot.game_agent_id,
                        task_snapshot.game_id,
                    )
                    if frozen is None:
                        raise ArenaTaskNotFoundError(
                            "Arena Game Agent does not exist or is inactive"
                        )
                    frozen_data = _mapping(frozen)
                    frozen_config = secure_config_snapshot(
                        _json_object(frozen_data["config_snapshot"])
                    )
                    if (
                        frozen_data["config_hash"] != config_hash
                        or canonical_json_bytes(frozen_config)
                        != canonical_json_bytes(secured_config)
                    ):
                        raise ArenaIdempotencyConflictError(
                            "Task config does not match the frozen Game Agent config"
                        )
                    credential_id = frozen_data["credential_id"]
                    if (
                        frozen_data["runtime_kind"] == "hosted"
                        and credential_id is None
                    ):
                        raise ArenaIdempotencyConflictError(
                            "Hosted Game Agent has no frozen credential"
                        )
                    if frozen_data["runtime_kind"] == "hosted":
                        if secured_config.get("credential_id") != credential_id:
                            raise ArenaIdempotencyConflictError(
                                "Hosted credential does not match frozen config"
                            )
                    else:
                        if secured_config.get("credential_id") is not None:
                            raise ArenaIdempotencyConflictError(
                                "Local Runtime config contains a credential reference"
                            )
                        credential_id = None

                    inserted = await connection.fetchrow(
                        f"""
                        INSERT INTO arena_agent_tasks (
                            task_id,
                            task_kind,
                            schema_version,
                            game_id,
                            round_id,
                            game_agent_id,
                            runtime_binding_id,
                            credential_id,
                            negotiation_id,
                            turn_sequence,
                            deadline_at,
                            idempotency_key,
                            input_snapshot,
                            input_hash,
                            runtime_config_snapshot,
                            config_hash,
                            default_result_id,
                            default_result_hash
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9,
                            $10, $11, $12, $13::jsonb, $14,
                            $15::jsonb, $16, $17, $18
                        )
                        ON CONFLICT (game_agent_id, idempotency_key) DO NOTHING
                        RETURNING {_TASK_COLUMNS}
                        """,
                        task_snapshot.task_id,
                        task_snapshot.kind,
                        task_snapshot.schema_version,
                        task_snapshot.game_id,
                        task_snapshot.round_id,
                        task_snapshot.game_agent_id,
                        frozen_data["runtime_binding_id"],
                        credential_id,
                        task_snapshot.negotiation_id,
                        turn_sequence,
                        task_snapshot.deadline_at,
                        task_snapshot.idempotency_key,
                        input_parameter,
                        task_snapshot.input_hash,
                        config_parameter,
                        config_hash,
                        default_result_id,
                        default_result_hash,
                    )
                    if inserted is None:
                        inserted = await connection.fetchrow(
                            f"""
                            SELECT {_TASK_COLUMNS}
                            FROM arena_agent_tasks
                            WHERE game_agent_id = $1
                              AND idempotency_key = $2
                            FOR SHARE
                            """,
                            task_snapshot.game_agent_id,
                            task_snapshot.idempotency_key,
                        )
                        if inserted is None:
                            raise ArenaIdempotencyConflictError(
                                "Arena task idempotency lookup failed"
                            )
                        existing = _task_record(inserted)
                        if (
                            existing.task.kind != task_snapshot.kind
                            or existing.task.input_hash
                            != task_snapshot.input_hash
                            or existing.config_hash != config_hash
                        ):
                            raise ArenaIdempotencyConflictError(
                                "Arena task idempotency key conflicts"
                            )
                        return existing

                    created = _task_record(inserted)
                    await connection.execute(
                        """
                        INSERT INTO arena_agent_task_events (
                            event_id,
                            task_id,
                            event_type,
                            created_at,
                            safe_metadata
                        )
                        VALUES (
                            $1,
                            $2,
                            'created',
                            $3,
                            $4::jsonb
                        )
                        ON CONFLICT (event_id) DO NOTHING
                        """,
                        f"{task_snapshot.task_id}:event:created",
                        task_snapshot.task_id,
                        created.created_at,
                        {
                            "kind": task_snapshot.kind,
                            "input_hash": task_snapshot.input_hash,
                            "config_hash": config_hash,
                        },
                    )
                    return created
        except (
            ArenaIdempotencyConflictError,
            ArenaRepositoryError,
            ArenaTaskNotFoundError,
        ):
            raise
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise ArenaIdempotencyConflictError(
                    "Arena task uniqueness conflict"
                ) from exc
            raise

    async def get_task(self, task_id: str) -> ArenaTaskRecord | None:
        pool = self._require_pool()
        row = await pool.fetchrow(
            f"""
            SELECT {_TASK_COLUMNS}
            FROM arena_agent_tasks
            WHERE task_id = $1
            """,
            task_id,
        )
        return None if row is None else _task_record(row)

    async def claim_connector_tasks(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: int,
    ) -> tuple[ConnectorTaskClaim, ...]:
        if not worker_id or len(worker_id) > 200:
            raise ValueError("Connector dispatcher worker_id is invalid")
        if limit < 1 or limit > 50:
            raise ValueError("Connector task claim limit must be between 1 and 50")
        if lease_seconds < 1 or lease_seconds > 600:
            raise ValueError("Connector task lease must be between 1 and 600 seconds")
        rows = await self._require_pool().fetch(
            """
            WITH candidates AS (
                SELECT t.task_id
                FROM arena_agent_tasks AS t
                JOIN arena_runtime_bindings AS b
                  ON b.runtime_binding_id = t.runtime_binding_id
                WHERE t.deadline_at > clock_timestamp()
                  AND b.runtime_kind = 'connector'
                  AND b.route_status = 'ready'
                  AND b.disabled_at IS NULL
                  AND (
                      t.status = 'queued'
                      OR (
                          t.status = 'leased'
                          AND t.lease_expires_at <= clock_timestamp()
                      )
                  )
                ORDER BY t.deadline_at, t.created_at, t.task_id
                FOR UPDATE OF t SKIP LOCKED
                LIMIT $2
            ),
            updated AS (
                UPDATE arena_agent_tasks AS t
                SET status = 'leased',
                    leased_by = $1,
                    lease_expires_at = (
                        clock_timestamp()
                        + $3 * interval '1 second'
                    )
                FROM candidates AS c
                WHERE t.task_id = c.task_id
                RETURNING t.*
            ),
            event_rows AS (
                INSERT INTO arena_agent_task_events (
                    event_id,
                    task_id,
                    event_type,
                    created_at,
                    safe_metadata
                )
                SELECT
                    u.task_id || ':event:connector-leased:'
                        || txid_current()::text,
                    u.task_id,
                    'leased',
                    clock_timestamp(),
                    jsonb_build_object('worker_id', $1)
                FROM updated AS u
                ON CONFLICT (event_id) DO NOTHING
                RETURNING task_id
            )
            SELECT
                u.*,
                b.connector_binding_id,
                b.connector_binding_epoch
            FROM updated AS u
            JOIN arena_runtime_bindings AS b
              ON b.runtime_binding_id = u.runtime_binding_id
            ORDER BY u.deadline_at, u.task_id
            """,
            worker_id,
            limit,
            lease_seconds,
        )
        return tuple(
            ConnectorTaskClaim(
                task=_task_record(row).task,
                connector_binding_id=str(row["connector_binding_id"]),
                connector_binding_epoch=int(
                    row["connector_binding_epoch"]
                ),
            )
            for row in rows
        )

    async def defer_connector_task(
        self,
        *,
        task_id: str,
        worker_id: str,
        delay_seconds: int,
    ) -> None:
        if delay_seconds < 1 or delay_seconds > 600:
            raise ValueError(
                "Connector task defer delay must be between 1 and 600 seconds"
            )
        await self._require_pool().fetchval(
            """
            UPDATE arena_agent_tasks
            SET lease_expires_at = (
                clock_timestamp()
                + $3 * interval '1 second'
            )
            WHERE task_id = $1
              AND leased_by = $2
              AND status = 'leased'
              AND deadline_at > clock_timestamp()
            RETURNING true
            """,
            task_id,
            worker_id,
            delay_seconds,
        )

    async def get_result_for_task(
        self, task_id: str
    ) -> ArenaResultRecord | None:
        pool = self._require_pool()
        row = await pool.fetchrow(
            f"""
            SELECT {_RESULT_COLUMNS}
            FROM arena_agent_task_results
            WHERE task_id = $1
            """,
            task_id,
        )
        return None if row is None else _result_record(row)

    async def submit_result(
        self,
        *,
        result: AgentTaskResultV1,
        server_clock: Callable[[], datetime],
        message_replaced: bool,
        public_output_policy_version: str | None,
    ) -> ResultSubmissionReceipt:
        del server_clock  # The SQL function owns clock_timestamp().
        validate_runtime_result_identifiers(result)

        runtime_result_id_digest = sha256_text_identifier(result.result_id)
        result_hash = sha256_identifier(result)
        candidate_action = (
            None
            if result.action is None
            else result.action.model_dump(mode="json", by_alias=True)
        )
        error_class = (
            None if result.status == "succeeded" else f"runtime_{result.status}"
        )
        pool = self._require_pool()
        try:
            row = await pool.fetchrow(
                """
                SELECT *
                FROM submit_agent_task_result(
                    $1,
                    $2,
                    $3,
                    $4,
                    $5,
                    $6::jsonb,
                    $7,
                    $8,
                    $9
                )
                """,
                result.task_id,
                runtime_result_id_digest,
                result_hash,
                result.schema_version,
                result.status,
                candidate_action,
                message_replaced,
                public_output_policy_version,
                error_class,
            )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "P0002":
                raise ArenaTaskNotFoundError("Arena task does not exist") from exc
            raise
        if row is None:
            raise ArenaRepositoryError("Result Sink returned no disposition")

        data = _mapping(row)
        disposition = data["disposition"]
        if disposition == "conflict":
            raise ArenaResultConflictError(
                "Runtime result id conflicts with an existing result"
            )
        try:
            resolved_disposition = SubmissionDisposition(disposition)
            task_status = TaskStatus(data["terminal_task_status"])
        except ValueError as exc:
            raise ArenaRepositoryError(
                "Result Sink returned an unknown status"
            ) from exc
        return ResultSubmissionReceipt(
            task_id=result.task_id,
            disposition=resolved_disposition,
            authoritative_result_id=data["authoritative_result_id"],
            task_status=task_status,
            result_received_at=_aware_timestamp(
                data["result_received_at"], field="result_received_at"
            ),
        )

    async def finalize_expired(
        self,
        *,
        server_clock: Callable[[], datetime],
        limit: int,
    ) -> list[ArenaResultRecord]:
        del server_clock  # Selection and CAS both use PostgreSQL time.
        if limit <= 0:
            return []
        pool = self._require_pool()
        records: list[ArenaResultRecord] = []
        async with pool.acquire() as connection:
            async with connection.transaction():
                tasks = await connection.fetch(
                    """
                    SELECT task_id
                    FROM arena_agent_tasks
                    WHERE status IN ('queued', 'leased', 'running')
                      AND deadline_at <= clock_timestamp()
                    ORDER BY deadline_at, task_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                    """,
                    limit,
                )
                for task_row in tasks:
                    task_id = _mapping(task_row)["task_id"]
                    finalized = await connection.fetchval(
                        "SELECT finalize_expired_agent_task($1)",
                        task_id,
                    )
                    if not finalized:
                        continue
                    result_row = await connection.fetchrow(
                        f"""
                        SELECT {_RESULT_COLUMNS}
                        FROM arena_agent_task_results
                        WHERE task_id = $1
                        """,
                        task_id,
                    )
                    if result_row is None:
                        raise ArenaRepositoryError(
                            "Finalizer completed without a Result"
                        )
                    records.append(_result_record(result_row))
        return records

    async def pending_results(self, *, limit: int) -> list[ArenaResultRecord]:
        if limit <= 0:
            return []
        pool = self._require_pool()
        rows = await pool.fetch(
            f"""
            SELECT {_RESULT_COLUMNS}
            FROM arena_agent_task_results
            WHERE apply_status = 'pending'
            ORDER BY result_received_at, result_id
            LIMIT $1
            """,
            limit,
        )
        return [_result_record(row) for row in rows]

    async def apply_result(
        self,
        *,
        result_id: str,
        server_clock: Callable[[], datetime],
    ) -> AppliedArenaAction | None:
        del server_clock  # The apply function owns clock_timestamp().
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                applied = await connection.fetchval(
                    "SELECT apply_arena_agent_task_result($1)",
                    result_id,
                )
                if not applied:
                    exists = await connection.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM arena_agent_task_results
                            WHERE result_id = $1
                        )
                        """,
                        result_id,
                    )
                    if not exists:
                        raise ArenaResultConflictError(
                            "Arena result does not exist"
                        )
                    return None
                row = await connection.fetchrow(
                    f"""
                    SELECT {_APPLIED_COLUMNS}
                    FROM arena_applied_agent_actions
                    WHERE result_id = $1
                    """,
                    result_id,
                )
                if row is None:
                    raise ArenaRepositoryError(
                        "Applied Result has no business action"
                    )
                return _applied_action(row)

    async def list_events(self, task_id: str) -> list[TaskEventRecord]:
        pool = self._require_pool()
        rows = await pool.fetch(
            """
            SELECT event_id, task_id, event_type, created_at, safe_metadata
            FROM arena_agent_task_events
            WHERE task_id = $1
            ORDER BY created_at, event_id
            """,
            task_id,
        )
        return [
            TaskEventRecord(
                event_id=_mapping(row)["event_id"],
                task_id=_mapping(row)["task_id"],
                event_type=_mapping(row)["event_type"],
                created_at=_aware_timestamp(
                    _mapping(row)["created_at"], field="event created_at"
                ),
                data=_json_object(_mapping(row)["safe_metadata"]),
            )
            for row in rows
        ]

    async def list_applied_actions(self) -> list[AppliedArenaAction]:
        pool = self._require_pool()
        rows = await pool.fetch(
            f"""
            SELECT {_APPLIED_COLUMNS}
            FROM arena_applied_agent_actions
            ORDER BY applied_at, task_id
            """
        )
        return [_applied_action(row) for row in rows]

    @staticmethod
    def _default_result(task_id: str) -> AgentTaskResultV1:
        task_digest = sha256_text_identifier(task_id)[len("sha256:") :]
        return AgentTaskResultV1(
            result_id=f"default:{task_digest}",
            task_id=task_id,
            schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
            status="timed_out",
        )


__all__ = ["PostgresArenaCoreRepository"]
