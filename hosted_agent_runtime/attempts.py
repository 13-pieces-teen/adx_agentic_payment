"""Durable Attempt metadata shared by the PydanticAI Hosted Runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, TypeAlias, runtime_checkable

from .providers import (
    MAX_POSTGRES_BIGINT,
    ProviderErrorCode,
    ProviderUsage,
    validate_provider_error_code,
    validate_provider_request_id,
)


MAX_AGENT_TASK_ATTEMPTS = 2

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


def _require_aware(value: datetime, *, label: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{label} must be an aware datetime")
    return value


@dataclass(frozen=True, slots=True)
class AttemptCreated:
    """Small private execution record containing no Prompt or credential."""

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
            or not 1 <= self.attempt_number <= MAX_AGENT_TASK_ATTEMPTS
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
    """Persistence port implemented by Hosted Worker infrastructure."""

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
    """Test-only in-memory recorder; this is not durable storage."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._attempts: dict[str, _MutableAttempt] = {}
        self._order: list[str] = []

    @property
    def durable(self) -> Literal[False]:
        return False

    @property
    def records(self) -> tuple[AttemptRecord, ...]:
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


__all__ = [
    "AttemptCompletion",
    "AttemptCreated",
    "AttemptErrorCode",
    "AttemptRecord",
    "AttemptRecorder",
    "AttemptStatus",
    "AttemptTerminalStatus",
    "MAX_AGENT_TASK_ATTEMPTS",
    "MemoryAttemptRecorder",
]
