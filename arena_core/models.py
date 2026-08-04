"""Arena Core persistence records and state enums."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from arena_agent_contracts import (
    AgentTaskResultV1,
    ArenaAgentTaskV1,
    ArenaTaskKindV1,
)


class TaskStatus(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    COMPLETED = "completed"
    DEFAULTED = "defaulted"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            TaskStatus.COMPLETED,
            TaskStatus.DEFAULTED,
            TaskStatus.CANCELLED,
        }


class ResultApplyStatus(str, Enum):
    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"


class SubmissionDisposition(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    LATE = "late"


@dataclass(slots=True)
class ArenaTaskRecord:
    task: ArenaAgentTaskV1
    config_snapshot: dict[str, Any]
    config_hash: str
    status: TaskStatus
    created_at: datetime
    attempt_count: int = 0
    leased_by: str | None = None
    lease_expires_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ConnectorTaskClaim:
    task: ArenaAgentTaskV1
    connector_binding_id: str
    connector_binding_epoch: int


@dataclass(frozen=True, slots=True)
class ConnectorTaskRoute:
    task: ArenaAgentTaskV1
    connector_binding_id: str
    connector_binding_epoch: int
    status: TaskStatus
    leased_by: str | None
    lease_expires_at: datetime | None


@dataclass(slots=True)
class ArenaResultRecord:
    result: AgentTaskResultV1
    result_received_at: datetime
    result_hash: str
    apply_status: ResultApplyStatus = ResultApplyStatus.PENDING
    message_replaced: bool = False
    public_output_policy_version: str | None = None
    safe_error_class: str | None = None
    arena_applied_at: datetime | None = None
    arena_rejected_at: datetime | None = None
    rejection_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TaskEventRecord:
    event_id: str
    task_id: str
    event_type: str
    created_at: datetime
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResultSubmissionReceipt:
    task_id: str
    disposition: SubmissionDisposition
    authoritative_result_id: str | None
    task_status: TaskStatus
    result_received_at: datetime


@dataclass(frozen=True, slots=True)
class ArenaApplication:
    accepted: bool
    outcome: Literal[
        "candidate",
        "default_pass",
        "negotiation_timeout",
        "market_timeout",
        "cancelled",
    ]
    action: dict[str, Any] | None = None
    rejection_reason: str | None = None


@dataclass(frozen=True, slots=True)
class AppliedArenaAction:
    task_id: str
    result_id: str
    kind: ArenaTaskKindV1
    outcome: str
    action: dict[str, Any] | None
    entered_at: datetime
    applied_at: datetime
