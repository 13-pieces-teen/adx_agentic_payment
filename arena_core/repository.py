"""Arena Core repository contract and deterministic in-memory implementation."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Protocol

from arena_agent_contracts import (
    AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
    AgentTaskResultV1,
    ArenaAgentTaskV1,
)

from .models import (
    AppliedArenaAction,
    ArenaApplication,
    ArenaResultRecord,
    ArenaTaskRecord,
    ResultApplyStatus,
    ResultSubmissionReceipt,
    SubmissionDisposition,
    TaskEventRecord,
    TaskStatus,
)
from .application_policy import derive_application
from .hashing import sha256_identifier, sha256_text_identifier
from .ingress_security import (
    secure_config_snapshot,
    validate_runtime_result_identifiers,
)


class ArenaRepositoryError(Exception):
    pass


class ArenaTaskNotFoundError(ArenaRepositoryError):
    pass


class ArenaIdempotencyConflictError(ArenaRepositoryError):
    pass


class ArenaResultConflictError(ArenaRepositoryError):
    pass


class ArenaCoreRepository(Protocol):
    async def create_task(
        self,
        *,
        task: ArenaAgentTaskV1,
        config_snapshot: dict,
        config_hash: str,
        created_at: datetime,
    ) -> ArenaTaskRecord: ...

    async def get_task(self, task_id: str) -> ArenaTaskRecord | None: ...

    async def get_task_by_idempotency(
        self,
        *,
        game_agent_id: str,
        idempotency_key: str,
    ) -> ArenaTaskRecord | None: ...

    async def get_result_for_task(self, task_id: str) -> ArenaResultRecord | None: ...

    async def get_results_for_tasks(
        self, task_ids: list[str]
    ) -> dict[str, ArenaResultRecord]: ...

    async def submit_result(
        self,
        *,
        result: AgentTaskResultV1,
        server_clock: Callable[[], datetime],
        message_replaced: bool,
        public_output_policy_version: str | None,
    ) -> ResultSubmissionReceipt: ...

    async def finalize_expired(
        self, *, server_clock: Callable[[], datetime], limit: int
    ) -> list[ArenaResultRecord]: ...

    async def pending_results(self, *, limit: int) -> list[ArenaResultRecord]: ...

    async def apply_result(
        self,
        *,
        result_id: str,
        server_clock: Callable[[], datetime],
    ) -> AppliedArenaAction | None: ...

    async def list_events(self, task_id: str) -> list[TaskEventRecord]: ...

    async def list_applied_actions(self) -> list[AppliedArenaAction]: ...


class MemoryArenaCoreRepository:
    """Transaction-like in-memory repository used by contract tests.

    Every mutation is serialized by one lock and every returned record is a
    deep copy. This makes nested participant-view dictionaries immutable from
    the caller's perspective and mirrors the PostgreSQL CAS boundaries.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tasks: dict[str, ArenaTaskRecord] = {}
        self._task_by_idempotency: dict[tuple[str, str], str] = {}
        self._results: dict[str, ArenaResultRecord] = {}
        self._result_by_task: dict[str, str] = {}
        self._events: list[TaskEventRecord] = []
        self._applied_by_task: dict[str, AppliedArenaAction] = {}

    @staticmethod
    def _copy(value):
        return copy.deepcopy(value)

    @staticmethod
    def _server_now(server_clock: Callable[[], datetime]) -> datetime:
        now = server_clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Arena server clock must return an aware datetime")
        return now

    def _append_event(
        self,
        *,
        task_id: str,
        event_type: str,
        created_at: datetime,
        data: dict | None = None,
    ) -> None:
        self._events.append(
            TaskEventRecord(
                event_id=f"event_{uuid.uuid4().hex}",
                task_id=task_id,
                event_type=event_type,
                created_at=created_at,
                data=self._copy(data or {}),
            )
        )

    async def create_task(
        self,
        *,
        task: ArenaAgentTaskV1,
        config_snapshot: dict,
        config_hash: str,
        created_at: datetime,
    ) -> ArenaTaskRecord:
        task_snapshot = self._copy(task)
        secure_config_snapshot(
            task_snapshot.input.model_dump(
                mode="python",
                by_alias=True,
                exclude_none=False,
            )
        )
        config_snapshot_copy = secure_config_snapshot(config_snapshot)
        if sha256_identifier(task_snapshot.input) != task_snapshot.input_hash:
            raise ArenaIdempotencyConflictError(
                "Arena task input hash does not match its snapshot"
            )
        if sha256_identifier(config_snapshot_copy) != config_hash:
            raise ArenaIdempotencyConflictError(
                "Arena task config hash does not match its snapshot"
            )

        key = (task_snapshot.game_agent_id, task_snapshot.idempotency_key)
        async with self._lock:
            existing_id = self._task_by_idempotency.get(key)
            if existing_id is not None:
                existing = self._tasks[existing_id]
                if (
                    existing.task.input_hash != task_snapshot.input_hash
                    or existing.config_hash != config_hash
                    or existing.task.kind != task_snapshot.kind
                ):
                    raise ArenaIdempotencyConflictError(
                        "Arena task idempotency key was reused with a different snapshot"
                    )
                return self._copy(existing)

            if task_snapshot.task_id in self._tasks:
                raise ArenaIdempotencyConflictError("Arena task id already exists")

            record = ArenaTaskRecord(
                task=task_snapshot,
                config_snapshot=config_snapshot_copy,
                config_hash=config_hash,
                status=TaskStatus.QUEUED,
                created_at=created_at,
            )
            self._tasks[task_snapshot.task_id] = record
            self._task_by_idempotency[key] = task_snapshot.task_id
            self._append_event(
                task_id=task_snapshot.task_id,
                event_type="created",
                created_at=created_at,
                data={"kind": task_snapshot.kind},
            )
            return self._copy(record)

    async def get_task(self, task_id: str) -> ArenaTaskRecord | None:
        async with self._lock:
            record = self._tasks.get(task_id)
            return self._copy(record) if record else None

    async def get_task_by_idempotency(
        self,
        *,
        game_agent_id: str,
        idempotency_key: str,
    ) -> ArenaTaskRecord | None:
        async with self._lock:
            task_id = self._task_by_idempotency.get(
                (game_agent_id, idempotency_key)
            )
            if task_id is None:
                return None
            return self._copy(self._tasks[task_id])

    async def get_result_for_task(self, task_id: str) -> ArenaResultRecord | None:
        async with self._lock:
            result_id = self._result_by_task.get(task_id)
            return self._copy(self._results[result_id]) if result_id else None

    async def get_results_for_tasks(
        self, task_ids: list[str]
    ) -> dict[str, ArenaResultRecord]:
        async with self._lock:
            return {
                task_id: self._copy(self._results[result_id])
                for task_id in task_ids
                if (result_id := self._result_by_task.get(task_id)) is not None
            }

    def _create_timeout_result_locked(
        self, task: ArenaTaskRecord, now: datetime
    ) -> ArenaResultRecord:
        task_digest = hashlib.sha256(
            task.task.task_id.encode("utf-8")
        ).hexdigest()
        result_id = f"default:{task_digest}"
        result = AgentTaskResultV1(
            result_id=result_id,
            task_id=task.task.task_id,
            schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
            status="timed_out",
        )
        record = ArenaResultRecord(
            result=result,
            result_received_at=now,
            result_hash=sha256_identifier(result),
            safe_error_class="deadline_elapsed",
        )
        task.status = TaskStatus.DEFAULTED
        task.completed_at = now
        task.leased_by = None
        task.lease_expires_at = None
        self._results[result_id] = record
        self._result_by_task[task.task.task_id] = result_id
        self._append_event(
            task_id=task.task.task_id,
            event_type="defaulted",
            created_at=now,
            data={"reason": "deadline_elapsed"},
        )
        return record

    async def submit_result(
        self,
        *,
        result: AgentTaskResultV1,
        server_clock: Callable[[], datetime],
        message_replaced: bool,
        public_output_policy_version: str | None,
    ) -> ResultSubmissionReceipt:
        async with self._lock:
            received_at = self._server_now(server_clock)
            validate_runtime_result_identifiers(result)
            task = self._tasks.get(result.task_id)
            if task is None:
                raise ArenaTaskNotFoundError("Arena task does not exist")

            runtime_result_id_digest = sha256_text_identifier(result.result_id)
            internal_result_id = (
                "runtime:" + runtime_result_id_digest.removeprefix("sha256:")
            )
            incoming_hash = sha256_identifier(result)
            existing_with_id = self._results.get(internal_result_id)
            if existing_with_id is not None:
                if existing_with_id.result.task_id != result.task_id:
                    self._append_event(
                        task_id=result.task_id,
                        event_type="result_conflict",
                        created_at=received_at,
                        data={"incoming_result_hash": incoming_hash},
                    )
                    raise ArenaResultConflictError(
                        "Result id was reused for a different task"
                    )
                if existing_with_id.result_hash != incoming_hash:
                    self._append_event(
                        task_id=result.task_id,
                        event_type="result_conflict",
                        created_at=received_at,
                        data={
                            "authoritative_result_hash": existing_with_id.result_hash,
                            "incoming_result_hash": incoming_hash,
                        },
                    )
                    raise ArenaResultConflictError(
                        "Result id was replayed with a different terminal payload"
                    )
                authoritative_id = self._result_by_task.get(result.task_id)
                self._append_event(
                    task_id=result.task_id,
                    event_type="duplicate_result_ignored",
                    created_at=received_at,
                    data={"result_hash": existing_with_id.result_hash},
                )
                return ResultSubmissionReceipt(
                    task_id=result.task_id,
                    disposition=SubmissionDisposition.DUPLICATE,
                    authoritative_result_id=authoritative_id,
                    task_status=task.status,
                    result_received_at=existing_with_id.result_received_at,
                )

            authoritative_id = self._result_by_task.get(result.task_id)
            if authoritative_id is not None or task.status.terminal:
                self._append_event(
                    task_id=result.task_id,
                    event_type="late_result_ignored",
                    created_at=received_at,
                    data={"result_hash": incoming_hash, "reason": "task_terminal"},
                )
                return ResultSubmissionReceipt(
                    task_id=result.task_id,
                    disposition=SubmissionDisposition.LATE,
                    authoritative_result_id=authoritative_id,
                    task_status=task.status,
                    result_received_at=received_at,
                )

            if received_at >= task.task.deadline_at:
                timeout_record = self._create_timeout_result_locked(task, received_at)
                self._append_event(
                    task_id=result.task_id,
                    event_type="late_result_ignored",
                    created_at=received_at,
                    data={"result_hash": incoming_hash, "reason": "deadline_elapsed"},
                )
                return ResultSubmissionReceipt(
                    task_id=result.task_id,
                    disposition=SubmissionDisposition.LATE,
                    authoritative_result_id=timeout_record.result.result_id,
                    task_status=task.status,
                    result_received_at=received_at,
                )

            record = ArenaResultRecord(
                result=self._copy(
                    result.model_copy(update={"result_id": internal_result_id})
                ),
                result_received_at=received_at,
                result_hash=incoming_hash,
                message_replaced=message_replaced,
                public_output_policy_version=public_output_policy_version,
                safe_error_class=(
                    None if result.status == "succeeded" else f"runtime_{result.status}"
                ),
            )
            self._results[internal_result_id] = record
            self._result_by_task[result.task_id] = internal_result_id
            task.completed_at = received_at
            task.leased_by = None
            task.lease_expires_at = None
            if result.status == "succeeded":
                task.status = TaskStatus.COMPLETED
            elif result.status == "cancelled":
                task.status = TaskStatus.CANCELLED
            else:
                task.status = TaskStatus.DEFAULTED
            self._append_event(
                task_id=result.task_id,
                event_type="result_submitted",
                created_at=received_at,
                data={
                    "result_hash": incoming_hash,
                    "runtime_status": result.status,
                    "message_replaced": message_replaced,
                },
            )
            return ResultSubmissionReceipt(
                task_id=result.task_id,
                disposition=SubmissionDisposition.ACCEPTED,
                authoritative_result_id=internal_result_id,
                task_status=task.status,
                result_received_at=received_at,
            )

    async def finalize_expired(
        self, *, server_clock: Callable[[], datetime], limit: int
    ) -> list[ArenaResultRecord]:
        if limit <= 0:
            return []
        async with self._lock:
            now = self._server_now(server_clock)
            eligible = sorted(
                (
                    task
                    for task in self._tasks.values()
                    if not task.status.terminal
                    and task.task.deadline_at <= now
                    and task.task.task_id not in self._result_by_task
                ),
                key=lambda item: (item.task.deadline_at, item.task.task_id),
            )
            records = [
                self._create_timeout_result_locked(task, now)
                for task in eligible[:limit]
            ]
            return self._copy(records)

    async def pending_results(self, *, limit: int) -> list[ArenaResultRecord]:
        if limit <= 0:
            return []
        async with self._lock:
            records = sorted(
                (
                    record
                    for record in self._results.values()
                    if record.apply_status == ResultApplyStatus.PENDING
                ),
                key=lambda item: (
                    item.result_received_at,
                    item.result.result_id,
                ),
            )
            return self._copy(records[:limit])

    async def apply_result(
        self,
        *,
        result_id: str,
        server_clock: Callable[[], datetime],
    ) -> AppliedArenaAction | None:
        async with self._lock:
            applied_at = self._server_now(server_clock)
            result = self._results.get(result_id)
            if result is None:
                raise ArenaResultConflictError("Arena result does not exist")
            if result.apply_status == ResultApplyStatus.APPLIED:
                return None
            if result.apply_status == ResultApplyStatus.REJECTED:
                return None

            task = self._tasks[result.result.task_id]
            application = derive_application(task.task, result.result)
            if not application.accepted:
                result.apply_status = ResultApplyStatus.REJECTED
                result.arena_rejected_at = applied_at
                result.rejection_reason = application.rejection_reason
                self._append_event(
                    task_id=task.task.task_id,
                    event_type="result_rejected",
                    created_at=applied_at,
                    data={
                        "result_hash": result.result_hash,
                        "reason": application.rejection_reason or "invalid_candidate",
                    },
                )
                return None

            existing = self._applied_by_task.get(task.task.task_id)
            if existing is not None:
                if existing.result_id != result_id:
                    raise ArenaResultConflictError(
                        "Task already has a different applied action"
                    )
                result.apply_status = ResultApplyStatus.APPLIED
                result.arena_applied_at = applied_at
                return None

            entered_at = (
                result.result_received_at
                if application.outcome == "candidate"
                else applied_at
            )
            applied = AppliedArenaAction(
                task_id=task.task.task_id,
                result_id=result_id,
                kind=task.task.kind,
                outcome=application.outcome,
                action=self._copy(application.action),
                entered_at=entered_at,
                applied_at=applied_at,
            )
            self._applied_by_task[task.task.task_id] = applied
            result.apply_status = ResultApplyStatus.APPLIED
            result.arena_applied_at = applied_at
            result.rejection_reason = application.rejection_reason
            self._append_event(
                task_id=task.task.task_id,
                event_type="result_applied",
                created_at=applied_at,
                data={
                    "result_hash": result.result_hash,
                    "outcome": application.outcome,
                    "reason": application.rejection_reason,
                },
            )
            return self._copy(applied)

    async def list_events(self, task_id: str) -> list[TaskEventRecord]:
        async with self._lock:
            return self._copy(
                [event for event in self._events if event.task_id == task_id]
            )

    async def list_applied_actions(self) -> list[AppliedArenaAction]:
        async with self._lock:
            return self._copy(
                sorted(
                    self._applied_by_task.values(),
                    key=lambda item: (item.applied_at, item.task_id),
                )
            )

    async def seed_tasks(self, records: Iterable[ArenaTaskRecord]) -> None:
        """Testing helper that still preserves idempotency indexes."""

        async with self._lock:
            for source in records:
                record = self._copy(source)
                self._tasks[record.task.task_id] = record
                self._task_by_idempotency[
                    (record.task.game_agent_id, record.task.idempotency_key)
                ] = record.task.task_id
