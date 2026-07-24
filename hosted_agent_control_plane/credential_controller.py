"""Durable credential lifecycle controller.

This process owns only Tencent SSM disable/delete operations. It cannot read
secret plaintext, invoke a model, or mutate Arena business state.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from hosted_agent_runtime.secret_store import (
    SecretController,
    SecretReference,
    SecretStoreError,
)


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CredentialLifecycleJob:
    lifecycle_job_id: str
    credential_id: str
    job_kind: str
    secret_ref: str
    attempt_no: int
    max_attempts: int
    deadline_at: datetime


class CredentialLifecycleRepository(Protocol):
    async def claim(
        self,
        *,
        controller_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[CredentialLifecycleJob]: ...

    async def complete(
        self,
        *,
        lifecycle_job_id: str,
        controller_id: str,
        succeeded: bool,
        error_class: str | None,
        retry_at: datetime | None,
    ) -> str: ...


class PostgresCredentialLifecycleRepository:
    """Least-privilege PostgreSQL adapter for lifecycle jobs."""

    def __init__(self, database_url: str, *, pool: Any | None = None) -> None:
        if not database_url:
            raise ValueError("Credential Controller PostgreSQL DSN is required")
        self.database_url = database_url
        self._pool = pool
        self._owns_pool = pool is None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        try:
            import asyncpg
        except ImportError:
            raise RuntimeError(
                "asyncpg is required for Credential Controller"
            ) from None
        self._pool = await asyncpg.create_pool(
            self.database_url,
            min_size=1,
            max_size=2,
            command_timeout=30,
            setup=self._setup_connection,
        )
        self._owns_pool = True

    async def close(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
            self._pool = None

    @staticmethod
    async def _setup_connection(connection: Any) -> None:
        await connection.execute("SET ROLE adx_credential_controller")
        await connection.execute("SET search_path TO pg_catalog, public")

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError(
                "Credential Controller repository is not initialized"
            )
        return self._pool

    async def claim(
        self,
        *,
        controller_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[CredentialLifecycleJob]:
        rows = await self._require_pool().fetch(
            """
            SELECT *
            FROM claim_credential_lifecycle_jobs($1, $2, $3)
            """,
            controller_id,
            limit,
            lease_seconds,
        )
        return [
            CredentialLifecycleJob(
                lifecycle_job_id=row["lifecycle_job_id"],
                credential_id=row["credential_id"],
                job_kind=row["job_kind"],
                secret_ref=row["secret_ref"],
                attempt_no=int(row["attempt_no"]),
                max_attempts=int(row["max_attempts"]),
                deadline_at=row["deadline_at"],
            )
            for row in rows
        ]

    async def complete(
        self,
        *,
        lifecycle_job_id: str,
        controller_id: str,
        succeeded: bool,
        error_class: str | None,
        retry_at: datetime | None,
    ) -> str:
        return str(
            await self._require_pool().fetchval(
                """
                SELECT complete_credential_lifecycle_job(
                    $1, $2, $3, $4, $5
                )
                """,
                lifecycle_job_id,
                controller_id,
                succeeded,
                error_class,
                retry_at,
            )
        )


class DurableCredentialController:
    """Claim and execute revoke/delete jobs without plaintext read access."""

    def __init__(
        self,
        *,
        repository: CredentialLifecycleRepository,
        secret_controller: SecretController,
        controller_id: str | None = None,
        lease_seconds: int = 60,
        claim_limit: int = 10,
        retry_seconds: int = 30,
    ) -> None:
        if not 1 <= lease_seconds <= 600:
            raise ValueError("lease_seconds must be between 1 and 600")
        if not 1 <= claim_limit <= 20:
            raise ValueError("claim_limit must be between 1 and 20")
        if retry_seconds < 1:
            raise ValueError("retry_seconds must be positive")
        if any(
            hasattr(secret_controller, operation)
            for operation in ("create", "resolve_for_worker")
        ):
            raise TypeError(
                "Credential Controller must not receive writer/reader ports"
            )
        self._repository = repository
        self._secret_controller = secret_controller
        self._controller_id = controller_id or (
            f"credential-controller-{uuid.uuid4().hex[:12]}"
        )
        self._lease_seconds = lease_seconds
        self._claim_limit = claim_limit
        self._retry_seconds = retry_seconds
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run_forever(self, *, poll_seconds: float = 1.0) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        while not self._stopping.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.error("credential_controller_cycle_failed")
                processed = 0
            if processed == 0:
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=poll_seconds,
                    )
                except TimeoutError:
                    pass

    async def run_once(self) -> int:
        jobs = await self._repository.claim(
            controller_id=self._controller_id,
            limit=self._claim_limit,
            lease_seconds=self._lease_seconds,
        )
        for job in jobs:
            await self._execute(job)
        return len(jobs)

    async def _execute(self, job: CredentialLifecycleJob) -> None:
        succeeded = False
        error_class: str | None = None
        try:
            secret_ref = SecretReference(job.secret_ref)
            if job.job_kind == "revoke":
                await self._secret_controller.revoke(secret_ref)
            elif job.job_kind == "delete":
                await self._secret_controller.delete_after_retention(
                    secret_ref
                )
            else:
                error_class = "unsupported_lifecycle_job"
            succeeded = error_class is None
        except SecretStoreError:
            error_class = "secret_store_operation_failed"
        except Exception:
            # Backend exception messages can contain request metadata. Persist
            # only a fixed safe class and let infrastructure logs carry a
            # correlation ID outside this business record.
            error_class = "credential_controller_unavailable"

        retry_at = self._retry_at(job) if not succeeded else None
        await self._repository.complete(
            lifecycle_job_id=job.lifecycle_job_id,
            controller_id=self._controller_id,
            succeeded=succeeded,
            error_class=error_class,
            retry_at=retry_at,
        )

    def _retry_at(self, job: CredentialLifecycleJob) -> datetime | None:
        now = datetime.now(timezone.utc)
        candidate = now + timedelta(seconds=self._retry_seconds)
        if (
            job.attempt_no >= job.max_attempts
            or candidate >= job.deadline_at
        ):
            return None
        return candidate


__all__ = [
    "CredentialLifecycleJob",
    "CredentialLifecycleRepository",
    "DurableCredentialController",
    "PostgresCredentialLifecycleRepository",
]
