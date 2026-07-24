"""Persistence contract for the Hosted Agent control plane.

A production implementation must durably enforce each idempotency digest
inside the same database transaction as its state mutation.  For a given
``(operation, owner_user_id, idempotency_key_digest)`` it must:

* resolve the owner-scoped current resource projection when the request hash
  is identical (provisioning status may have advanced asynchronously);
* reject a different request hash as a conflict;
* never redirect the digest to another resource;
* attach the pending resource to its reservation in the same transaction,
  then complete Credential HTTP idempotency with the ``stored`` transition; and
* atomically create Agent identity, Hosted Config, Runtime Binding, credential
  validation job, and the credential ``pending_validation`` transition.

Only non-secret domain models cross this port.  In particular, raw API keys
and ``SecretWrite`` values are deliberately absent from every method.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Literal, Protocol, TypeAlias, cast

from .models import (
    CredentialRecord,
    CredentialReservation,
    CredentialStatus,
    HostedAgentCreation,
    HostedAgentRecord,
    ReservationDisposition,
)


RepositoryErrorCode: TypeAlias = Literal[
    "credential_not_found",
    "credential_not_usable",
    "idempotency_conflict",
    "memory_repository_not_explicitly_test_only",
    "provider_mismatch",
]

_REPOSITORY_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "credential_not_found",
        "credential_not_usable",
        "idempotency_conflict",
        "memory_repository_not_explicitly_test_only",
        "provider_mismatch",
    }
)

_CREDENTIAL_OPERATION: Final[str] = "model_credentials.create"
_HOSTED_AGENT_OPERATION: Final[str] = "hosted_agents.create"
_HASH_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^sha256:[0-9a-f]{64}$"
)


class ControlRepositoryError(RuntimeError):
    """Safe repository error which never echoes request or owner values."""

    def __init__(self, code: RepositoryErrorCode) -> None:
        if type(code) is not str or code not in _REPOSITORY_ERROR_CODES:
            raise ValueError("invalid hosted control repository error code")
        self.code = cast(RepositoryErrorCode, code)
        super().__init__(f"Hosted control repository rejected operation ({code})")


class HostedAgentControlRepository(Protocol):
    """Durable persistence port; implementations own transaction boundaries."""

    @property
    def durable(self) -> bool: ...

    async def reserve_credential(
        self,
        *,
        credential: CredentialRecord,
        idempotency_key_digest: str,
        request_hash: str,
    ) -> CredentialReservation: ...

    async def mark_credential_stored_and_complete_idempotency(
        self,
        *,
        owner_user_id: str,
        credential_id: str,
        idempotency_key_digest: str,
        request_hash: str,
    ) -> CredentialRecord: ...

    async def get_credential_for_owner(
        self,
        *,
        owner_user_id: str,
        credential_id: str,
    ) -> CredentialRecord | None: ...

    async def list_credentials_for_owner(
        self,
        *,
        owner_user_id: str,
    ) -> tuple[CredentialRecord, ...]: ...

    async def get_hosted_agent_creation_replay(
        self,
        *,
        owner_user_id: str,
        idempotency_key_digest: str,
        request_hash: str,
    ) -> HostedAgentRecord | None: ...

    async def create_hosted_agent(
        self,
        *,
        agent: HostedAgentRecord,
        idempotency_key_digest: str,
        request_hash: str,
    ) -> HostedAgentCreation: ...

    async def get_hosted_agent_for_owner(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
    ) -> HostedAgentRecord | None: ...

    async def list_hosted_agents_for_owner(
        self,
        *,
        owner_user_id: str,
    ) -> tuple[HostedAgentRecord, ...]: ...


@dataclass(frozen=True, slots=True)
class _IdempotencyEntry:
    request_hash: str
    result_id: str


class MemoryHostedAgentControlRepository:
    """Explicitly test-only implementation of the persistence contract."""

    __slots__ = ("_agents", "_clock", "_credentials", "_idempotency", "_lock")

    def __init__(
        self,
        *,
        explicitly_test_only: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if explicitly_test_only is not True:
            raise ControlRepositoryError(
                "memory_repository_not_explicitly_test_only"
            )
        self._credentials: dict[str, CredentialRecord] = {}
        self._agents: dict[str, HostedAgentRecord] = {}
        self._idempotency: dict[
            tuple[str, str, str], _IdempotencyEntry
        ] = {}
        self._lock = asyncio.Lock()
        self._clock = clock

    @classmethod
    def for_testing(
        cls,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> "MemoryHostedAgentControlRepository":
        return cls(explicitly_test_only=True, clock=clock)

    @property
    def durable(self) -> bool:
        return False

    @staticmethod
    def _require_safe_hash(value: str) -> None:
        if (
            type(value) is not str
            or not _HASH_IDENTIFIER_PATTERN.fullmatch(value)
        ):
            raise ControlRepositoryError("idempotency_conflict")

    @staticmethod
    def _idempotency_result(
        *,
        entries: dict[tuple[str, str, str], _IdempotencyEntry],
        operation: str,
        owner_user_id: str,
        idempotency_key_digest: str,
        request_hash: str,
    ) -> _IdempotencyEntry | None:
        existing = entries.get(
            (operation, owner_user_id, idempotency_key_digest)
        )
        if existing is None:
            return None
        if existing.request_hash != request_hash:
            raise ControlRepositoryError("idempotency_conflict")
        return existing

    async def reserve_credential(
        self,
        *,
        credential: CredentialRecord,
        idempotency_key_digest: str,
        request_hash: str,
    ) -> CredentialReservation:
        self._require_safe_hash(idempotency_key_digest)
        self._require_safe_hash(request_hash)
        async with self._lock:
            replay = self._idempotency_result(
                entries=self._idempotency,
                operation=_CREDENTIAL_OPERATION,
                owner_user_id=credential.owner_user_id,
                idempotency_key_digest=idempotency_key_digest,
                request_hash=request_hash,
            )
            if replay is not None:
                return CredentialReservation(
                    disposition=ReservationDisposition.REPLAY,
                    credential=self._credentials[replay.result_id],
                )

            # A production repository also has database uniqueness constraints.
            if credential.credential_id in self._credentials:
                raise ControlRepositoryError("idempotency_conflict")
            self._credentials[credential.credential_id] = credential
            self._idempotency[
                (
                    _CREDENTIAL_OPERATION,
                    credential.owner_user_id,
                    idempotency_key_digest,
                )
            ] = _IdempotencyEntry(
                request_hash=request_hash,
                result_id=credential.credential_id,
            )
            return CredentialReservation(
                disposition=ReservationDisposition.CREATED,
                credential=credential,
            )

    async def mark_credential_stored_and_complete_idempotency(
        self,
        *,
        owner_user_id: str,
        credential_id: str,
        idempotency_key_digest: str,
        request_hash: str,
    ) -> CredentialRecord:
        self._require_safe_hash(idempotency_key_digest)
        self._require_safe_hash(request_hash)
        async with self._lock:
            replay = self._idempotency_result(
                entries=self._idempotency,
                operation=_CREDENTIAL_OPERATION,
                owner_user_id=owner_user_id,
                idempotency_key_digest=idempotency_key_digest,
                request_hash=request_hash,
            )
            if replay is None or replay.result_id != credential_id:
                raise ControlRepositoryError("idempotency_conflict")
            credential = self._credentials.get(credential_id)
            if credential is None:
                raise ControlRepositoryError("credential_not_found")
            if credential.owner_user_id != owner_user_id:
                raise ControlRepositoryError("credential_not_found")
            if credential.status is CredentialStatus.PENDING_WRITE:
                stored_credential = credential.model_copy(
                    update={
                        "status": CredentialStatus.STORED,
                        "updated_at": self._clock(),
                    }
                )
                self._credentials[credential_id] = stored_credential
                return stored_credential
            if credential.status is not CredentialStatus.STORED:
                raise ControlRepositoryError("credential_not_usable")
            return credential

    async def get_credential_for_owner(
        self,
        *,
        owner_user_id: str,
        credential_id: str,
    ) -> CredentialRecord | None:
        async with self._lock:
            credential = self._credentials.get(credential_id)
            if (
                credential is None
                or credential.owner_user_id != owner_user_id
            ):
                return None
            return credential

    async def list_credentials_for_owner(
        self,
        *,
        owner_user_id: str,
    ) -> tuple[CredentialRecord, ...]:
        async with self._lock:
            return tuple(
                sorted(
                    (
                        credential
                        for credential in self._credentials.values()
                        if credential.owner_user_id == owner_user_id
                    ),
                    key=lambda item: (
                        item.created_at,
                        item.credential_id,
                    ),
                    reverse=True,
                )
            )

    async def get_hosted_agent_creation_replay(
        self,
        *,
        owner_user_id: str,
        idempotency_key_digest: str,
        request_hash: str,
    ) -> HostedAgentRecord | None:
        """Return an exact completed replay before mutable prerequisites.

        Hosted Agent creation is one atomic repository mutation, so every
        in-memory idempotency entry for this operation is already completed.
        A production repository must return only a completed record here and
        leave in-progress/retryable handling to ``create_hosted_agent``.
        """

        self._require_safe_hash(idempotency_key_digest)
        self._require_safe_hash(request_hash)
        async with self._lock:
            replay = self._idempotency_result(
                entries=self._idempotency,
                operation=_HOSTED_AGENT_OPERATION,
                owner_user_id=owner_user_id,
                idempotency_key_digest=idempotency_key_digest,
                request_hash=request_hash,
            )
            if replay is None:
                return None
            agent = self._agents.get(replay.result_id)
            if agent is None or agent.owner_user_id != owner_user_id:
                # Never allow a stale/corrupt entry to redirect the key.
                raise ControlRepositoryError("idempotency_conflict")
            return agent

    async def create_hosted_agent(
        self,
        *,
        agent: HostedAgentRecord,
        idempotency_key_digest: str,
        request_hash: str,
    ) -> HostedAgentCreation:
        self._require_safe_hash(idempotency_key_digest)
        self._require_safe_hash(request_hash)
        async with self._lock:
            replay = self._idempotency_result(
                entries=self._idempotency,
                operation=_HOSTED_AGENT_OPERATION,
                owner_user_id=agent.owner_user_id,
                idempotency_key_digest=idempotency_key_digest,
                request_hash=request_hash,
            )
            if replay is not None:
                return HostedAgentCreation(
                    disposition=ReservationDisposition.REPLAY,
                    agent=self._agents[replay.result_id],
                )

            credential = self._credentials.get(agent.credential_id)
            # Owner mismatch is deliberately indistinguishable from absence.
            if (
                credential is None
                or credential.owner_user_id != agent.owner_user_id
            ):
                raise ControlRepositoryError("credential_not_found")
            if credential.provider_id != agent.provider_id:
                raise ControlRepositoryError("provider_mismatch")
            if credential.status is not CredentialStatus.STORED:
                raise ControlRepositoryError("credential_not_usable")
            if any(
                existing.credential_id == agent.credential_id
                for existing in self._agents.values()
            ):
                raise ControlRepositoryError("credential_not_usable")
            if agent.agent_id in self._agents:
                raise ControlRepositoryError("idempotency_conflict")

            # This block represents one production database transaction:
            # identity/config/binding/job insert + credential state transition.
            self._agents[agent.agent_id] = agent
            self._credentials[credential.credential_id] = credential.model_copy(
                update={
                    "status": CredentialStatus.PENDING_VALIDATION,
                    "updated_at": self._clock(),
                }
            )
            self._idempotency[
                (
                    _HOSTED_AGENT_OPERATION,
                    agent.owner_user_id,
                    idempotency_key_digest,
                )
            ] = _IdempotencyEntry(
                request_hash=request_hash,
                result_id=agent.agent_id,
            )
            return HostedAgentCreation(
                disposition=ReservationDisposition.CREATED,
                agent=agent,
            )

    async def get_hosted_agent_for_owner(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
    ) -> HostedAgentRecord | None:
        async with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None or agent.owner_user_id != owner_user_id:
                return None
            return agent

    async def list_hosted_agents_for_owner(
        self,
        *,
        owner_user_id: str,
    ) -> tuple[HostedAgentRecord, ...]:
        async with self._lock:
            return tuple(
                sorted(
                    (
                        agent
                        for agent in self._agents.values()
                        if agent.owner_user_id == owner_user_id
                    ),
                    key=lambda item: (item.created_at, item.agent_id),
                    reverse=True,
                )
            )


__all__ = [
    "ControlRepositoryError",
    "HostedAgentControlRepository",
    "MemoryHostedAgentControlRepository",
    "RepositoryErrorCode",
]
