"""Durable PostgreSQL implementation of the Hosted Agent control repository."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncIterator

try:
    import asyncpg
except ModuleNotFoundError:  # pragma: no cover - production dependency
    asyncpg = None  # type: ignore[assignment]

from .models import (
    AgentIdentityStatus,
    CredentialRecord,
    CredentialReservation,
    CredentialStatus,
    HostedAgentCreation,
    HostedAgentRecord,
    HostedProvisioningStatus,
    ReservationDisposition,
)
from .repository import ControlRepositoryError


_CREDENTIAL_ROUTE = "model_credentials.create"
_AGENT_ROUTE = "hosted_agents.create"
_AGENT_UPDATE_ROUTE = "hosted_agents.update"
_IDEMPOTENCY_TTL_SECONDS = 3600
_VALIDATION_DEADLINE_MINUTES = 10

_CREDENTIAL_SELECT = """
SELECT
    credential_id,
    owner_user_id,
    provider,
    secret_ref,
    fingerprint,
    fingerprint_pepper_version,
    status,
    created_at,
    updated_at
FROM arena_model_credentials
"""

_AGENT_SELECT = """
SELECT
    a.agent_id,
    a.owner_user_id,
    a.name,
    a.status AS identity_status,
    c.hosted_config_id,
    c.credential_id,
    c.provider,
    c.model,
    c.thinking_enabled,
    c.strategy_instructions,
    c.prompt_version,
    c.task_schema_version,
    c.action_schema_version,
    c.capability_version,
    c.adapter_version,
    c.max_input_bytes,
    c.max_context_items,
    c.max_output_tokens,
    c.config_hash,
    CASE
        WHEN a.runtime_update_job_id IS NOT NULL THEN 'provisioning'
        ELSE c.status
    END AS provisioning_status,
    b.runtime_binding_id,
    CASE
        WHEN a.runtime_update_job_id IS NOT NULL THEN 'provisioning'
        ELSE b.route_status
    END AS route_status,
    COALESCE(a.runtime_update_job_id, latest.validation_job_id)
        AS validation_job_id,
    a.created_at,
    GREATEST(a.updated_at, c.updated_at, b.updated_at) AS updated_at
FROM arena_agents AS a
JOIN arena_hosted_configs AS c ON c.agent_id = a.agent_id
JOIN arena_runtime_bindings AS b
  ON b.agent_id = a.agent_id
 AND b.hosted_config_id = c.hosted_config_id
LEFT JOIN LATERAL (
    SELECT j.validation_job_id
    FROM hosted_credential_validation_jobs AS j
    WHERE j.agent_id = a.agent_id
    ORDER BY j.created_at DESC, j.validation_job_id DESC
    LIMIT 1
) AS latest ON TRUE
"""


def _value(row: Mapping[str, Any], key: str) -> Any:
    return row[key]


def _credential(row: Mapping[str, Any]) -> CredentialRecord:
    return CredentialRecord(
        credential_id=_value(row, "credential_id"),
        owner_user_id=_value(row, "owner_user_id"),
        provider_id=_value(row, "provider"),
        secret_ref=_value(row, "secret_ref"),
        fingerprint=str(_value(row, "fingerprint")).strip(),
        fingerprint_pepper_version=_value(
            row, "fingerprint_pepper_version"
        ),
        status=CredentialStatus(_value(row, "status")),
        created_at=_value(row, "created_at"),
        updated_at=_value(row, "updated_at"),
    )


def _agent(row: Mapping[str, Any]) -> HostedAgentRecord:
    validation_job_id = _value(row, "validation_job_id")
    if not isinstance(validation_job_id, str) or not validation_job_id:
        raise ControlRepositoryError("idempotency_conflict")
    return HostedAgentRecord(
        agent_id=_value(row, "agent_id"),
        owner_user_id=_value(row, "owner_user_id"),
        display_name=_value(row, "name"),
        identity_status=AgentIdentityStatus(_value(row, "identity_status")),
        hosted_config_id=_value(row, "hosted_config_id"),
        credential_id=_value(row, "credential_id"),
        provider_id=_value(row, "provider"),
        model_id=_value(row, "model"),
        thinking_enabled=_value(row, "thinking_enabled"),
        strategy_instructions=_value(row, "strategy_instructions"),
        prompt_version=_value(row, "prompt_version"),
        task_schema_version=_value(row, "task_schema_version"),
        action_schema_version=_value(row, "action_schema_version"),
        capability_version=_value(row, "capability_version"),
        adapter_version=_value(row, "adapter_version"),
        max_input_bytes=_value(row, "max_input_bytes"),
        max_context_items=_value(row, "max_context_items"),
        max_output_tokens=_value(row, "max_output_tokens"),
        config_hash=_value(row, "config_hash"),
        provisioning_status=HostedProvisioningStatus(
            _value(row, "provisioning_status")
        ),
        runtime_binding_id=_value(row, "runtime_binding_id"),
        route_status=HostedProvisioningStatus(_value(row, "route_status")),
        validation_job_id=validation_job_id,
        created_at=_value(row, "created_at"),
        updated_at=_value(row, "updated_at"),
    )


class PostgresHostedAgentControlRepository:
    """One API-role pool; every multi-table mutation is one transaction."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        pool: object | None = None,
    ) -> None:
        if pool is None and (not isinstance(dsn, str) or not dsn):
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn
        self._pool = pool
        self._owns_pool = pool is None

    @property
    def durable(self) -> bool:
        return True

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        if asyncpg is None:
            raise RuntimeError("asyncpg is required for PostgreSQL repository")

        async def initialize_connection(connection: Any) -> None:
            await connection.execute("SET ROLE adx_arena_api")
            role = await connection.fetchval("SELECT current_role")
            if role != "adx_arena_api":
                raise RuntimeError("PostgreSQL Arena API role was not assumed")

        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=10,
            command_timeout=30,
            init=initialize_connection,
        )

    async def close(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
        self._pool = None

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        if self._pool is None:
            raise RuntimeError("PostgreSQL repository is not initialized")
        async with self._pool.acquire() as connection:
            yield connection

    async def reserve_credential(
        self,
        *,
        credential: CredentialRecord,
        idempotency_key_digest: str,
        request_hash: str,
    ) -> CredentialReservation:
        async with self._connection() as connection:
            async with connection.transaction():
                reservation = await connection.fetchrow(
                    """
                    SELECT * FROM reserve_arena_api_idempotency(
                        $1, $2, $3, $4, $5
                    )
                    """,
                    credential.owner_user_id,
                    _CREDENTIAL_ROUTE,
                    idempotency_key_digest,
                    request_hash,
                    _IDEMPOTENCY_TTL_SECONDS,
                )
                disposition = reservation["disposition"]
                if disposition == "conflict":
                    raise ControlRepositoryError("idempotency_conflict")
                resource_id = reservation["resource_id"]
                if disposition in {"replay", "retry", "in_progress"}:
                    if not resource_id:
                        raise ControlRepositoryError("idempotency_conflict")
                    row = await connection.fetchrow(
                        _CREDENTIAL_SELECT
                        + " WHERE owner_user_id = $1 AND credential_id = $2",
                        credential.owner_user_id,
                        resource_id,
                    )
                    if row is None:
                        raise ControlRepositoryError(
                            "idempotency_conflict"
                        )
                    return CredentialReservation(
                        disposition=ReservationDisposition.REPLAY,
                        credential=_credential(row),
                    )
                if disposition != "reserved":
                    raise ControlRepositoryError("idempotency_conflict")

                await connection.execute(
                    """
                    INSERT INTO arena_model_credentials (
                        credential_id,
                        owner_user_id,
                        provider,
                        secret_ref,
                        fingerprint,
                        fingerprint_pepper_version,
                        status,
                        unbound_expires_at,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, 'pending_write',
                        $7, $8, $8
                    )
                    """,
                    credential.credential_id,
                    credential.owner_user_id,
                    credential.provider_id,
                    credential.secret_ref,
                    credential.fingerprint,
                    credential.fingerprint_pepper_version,
                    credential.created_at + timedelta(hours=24),
                    credential.created_at,
                )
                attached = await connection.fetchrow(
                    """
                    SELECT * FROM attach_arena_api_idempotency_resource(
                        $1, $2, $3, $4, 'model_credential', $5
                    )
                    """,
                    credential.owner_user_id,
                    _CREDENTIAL_ROUTE,
                    idempotency_key_digest,
                    request_hash,
                    credential.credential_id,
                )
                if attached["disposition"] not in {"attached", "replay"}:
                    raise ControlRepositoryError("idempotency_conflict")
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
        async with self._connection() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE arena_model_credentials
                    SET status = 'stored',
                        updated_at = clock_timestamp()
                    WHERE owner_user_id = $1
                      AND credential_id = $2
                      AND status IN ('pending_write', 'stored')
                    RETURNING
                        credential_id, owner_user_id, provider, secret_ref,
                        fingerprint, fingerprint_pepper_version, status,
                        created_at, updated_at
                    """,
                    owner_user_id,
                    credential_id,
                )
                if row is None:
                    raise ControlRepositoryError("credential_not_usable")
                completed = await connection.fetchrow(
                    """
                    SELECT * FROM complete_arena_api_idempotency(
                        $1, $2, $3, $4, 'model_credential', $5, 201
                    )
                    """,
                    owner_user_id,
                    _CREDENTIAL_ROUTE,
                    idempotency_key_digest,
                    request_hash,
                    credential_id,
                )
                if completed["disposition"] not in {
                    "completed",
                    "replay",
                }:
                    raise ControlRepositoryError("idempotency_conflict")
                return _credential(row)

    async def get_credential_for_owner(
        self,
        *,
        owner_user_id: str,
        credential_id: str,
    ) -> CredentialRecord | None:
        async with self._connection() as connection:
            row = await connection.fetchrow(
                _CREDENTIAL_SELECT
                + " WHERE owner_user_id = $1 AND credential_id = $2",
                owner_user_id,
                credential_id,
            )
        return _credential(row) if row is not None else None

    async def list_credentials_for_owner(
        self,
        *,
        owner_user_id: str,
    ) -> tuple[CredentialRecord, ...]:
        async with self._connection() as connection:
            rows = await connection.fetch(
                _CREDENTIAL_SELECT
                + """
                  WHERE owner_user_id = $1
                  ORDER BY created_at DESC, credential_id DESC
                  LIMIT 100
                """,
                owner_user_id,
            )
        return tuple(_credential(row) for row in rows)

    async def get_hosted_agent_creation_replay(
        self,
        *,
        owner_user_id: str,
        idempotency_key_digest: str,
        request_hash: str,
    ) -> HostedAgentRecord | None:
        async with self._connection() as connection:
            replay = await connection.fetchrow(
                """
                SELECT * FROM lookup_completed_arena_api_idempotency(
                    $1, $2, $3, $4
                )
                """,
                owner_user_id,
                _AGENT_ROUTE,
                idempotency_key_digest,
                request_hash,
            )
            if replay["disposition"] == "conflict":
                raise ControlRepositoryError("idempotency_conflict")
            if replay["disposition"] != "replay":
                return None
            row = await connection.fetchrow(
                _AGENT_SELECT
                + " WHERE a.owner_user_id = $1 AND a.agent_id = $2",
                owner_user_id,
                replay["resource_id"],
            )
        if row is None:
            raise ControlRepositoryError("idempotency_conflict")
        return _agent(row)

    async def create_hosted_agent(
        self,
        *,
        agent: HostedAgentRecord,
        idempotency_key_digest: str,
        request_hash: str,
    ) -> HostedAgentCreation:
        async with self._connection() as connection:
            async with connection.transaction():
                reservation = await connection.fetchrow(
                    """
                    SELECT * FROM reserve_arena_api_idempotency(
                        $1, $2, $3, $4, $5
                    )
                    """,
                    agent.owner_user_id,
                    _AGENT_ROUTE,
                    idempotency_key_digest,
                    request_hash,
                    _IDEMPOTENCY_TTL_SECONDS,
                )
                disposition = reservation["disposition"]
                if disposition == "conflict":
                    raise ControlRepositoryError("idempotency_conflict")
                if disposition in {"replay", "retry", "in_progress"}:
                    resource_id = reservation["resource_id"]
                    if not resource_id:
                        raise ControlRepositoryError(
                            "idempotency_conflict"
                        )
                    row = await connection.fetchrow(
                        _AGENT_SELECT
                        + " WHERE a.owner_user_id = $1 AND a.agent_id = $2",
                        agent.owner_user_id,
                        resource_id,
                    )
                    if row is None:
                        raise ControlRepositoryError(
                            "idempotency_conflict"
                        )
                    return HostedAgentCreation(
                        disposition=ReservationDisposition.REPLAY,
                        agent=_agent(row),
                    )
                if disposition != "reserved":
                    raise ControlRepositoryError("idempotency_conflict")

                credential = await connection.fetchrow(
                    """
                    SELECT credential_id, owner_user_id, provider, status
                    FROM arena_model_credentials
                    WHERE credential_id = $1 AND owner_user_id = $2
                    FOR UPDATE
                    """,
                    agent.credential_id,
                    agent.owner_user_id,
                )
                if credential is None:
                    raise ControlRepositoryError("credential_not_found")
                if credential["provider"] != agent.provider_id:
                    raise ControlRepositoryError("provider_mismatch")
                if credential["status"] != "stored":
                    raise ControlRepositoryError("credential_not_usable")

                await connection.execute(
                    """
                    INSERT INTO arena_agents (
                        agent_id, owner_user_id, name, status,
                        runtime_update_job_id, created_at, updated_at
                    )
                    VALUES ($1, $2, $3, 'active', NULL, $4, $4)
                    """,
                    agent.agent_id,
                    agent.owner_user_id,
                    agent.display_name,
                    agent.created_at,
                )
                await connection.execute(
                    """
                    INSERT INTO arena_hosted_configs (
                        hosted_config_id, agent_id, owner_user_id,
                        credential_id, provider, model, thinking_enabled,
                        strategy_instructions, prompt_version,
                        task_schema_version, action_schema_version,
                        capability_version, adapter_version, max_input_bytes,
                        max_context_items, max_output_tokens, config_hash,
                        status, created_at, updated_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        $12, $13, $14, $15, $16, $17, 'provisioning',
                        $18, $18
                    )
                    """,
                    agent.hosted_config_id,
                    agent.agent_id,
                    agent.owner_user_id,
                    agent.credential_id,
                    agent.provider_id,
                    agent.model_id,
                    agent.thinking_enabled,
                    agent.strategy_instructions,
                    agent.prompt_version,
                    agent.task_schema_version,
                    agent.action_schema_version,
                    agent.capability_version,
                    agent.adapter_version,
                    agent.max_input_bytes,
                    agent.max_context_items,
                    agent.max_output_tokens,
                    agent.config_hash,
                    agent.created_at,
                )
                await connection.execute(
                    """
                    INSERT INTO arena_runtime_bindings (
                        runtime_binding_id, agent_id, runtime_kind,
                        hosted_config_id, route_status, created_at, updated_at
                    )
                    VALUES ($1, $2, 'hosted', $3, 'provisioning', $4, $4)
                    """,
                    agent.runtime_binding_id,
                    agent.agent_id,
                    agent.hosted_config_id,
                    agent.created_at,
                )

                candidate = {
                    "credential_id": agent.credential_id,
                    "provider": agent.provider_id,
                    "model": agent.model_id,
                    "thinking_enabled": agent.thinking_enabled,
                    "strategy_instructions": agent.strategy_instructions,
                    "prompt_version": agent.prompt_version,
                    "task_schema_version": agent.task_schema_version,
                    "action_schema_version": agent.action_schema_version,
                    "capability_version": agent.capability_version,
                    "adapter_version": agent.adapter_version,
                    "max_input_bytes": agent.max_input_bytes,
                    "max_context_items": agent.max_context_items,
                    "max_output_tokens": agent.max_output_tokens,
                }
                await connection.execute(
                    """
                    INSERT INTO hosted_credential_validation_jobs (
                        validation_job_id, agent_id, credential_id,
                        hosted_config_id, job_kind, candidate_config_snapshot,
                        candidate_config_hash, expected_current_config_hash,
                        validation_schema_version, status, max_attempts,
                        next_attempt_at, deadline_at, created_at
                    )
                    VALUES (
                        $1, $2, $3, $4, 'create', $5::jsonb, $6, $6,
                        'arena.credential-validation.v1', 'queued', 3,
                        $7::timestamptz,
                        $7::timestamptz + make_interval(mins => $8::integer),
                        $7::timestamptz
                    )
                    """,
                    agent.validation_job_id,
                    agent.agent_id,
                    agent.credential_id,
                    agent.hosted_config_id,
                    json.dumps(candidate, separators=(",", ":")),
                    agent.config_hash,
                    agent.created_at,
                    _VALIDATION_DEADLINE_MINUTES,
                )
                await connection.execute(
                    """
                    UPDATE arena_agents
                    SET runtime_update_job_id = $1, updated_at = $2
                    WHERE agent_id = $3
                    """,
                    agent.validation_job_id,
                    agent.created_at,
                    agent.agent_id,
                )
                await connection.execute(
                    """
                    UPDATE arena_model_credentials
                    SET status = 'pending_validation',
                        unbound_expires_at = NULL,
                        updated_at = $1
                    WHERE credential_id = $2
                    """,
                    agent.created_at,
                    agent.credential_id,
                )
                attached = await connection.fetchrow(
                    """
                    SELECT * FROM attach_arena_api_idempotency_resource(
                        $1, $2, $3, $4, 'arena_agent', $5
                    )
                    """,
                    agent.owner_user_id,
                    _AGENT_ROUTE,
                    idempotency_key_digest,
                    request_hash,
                    agent.agent_id,
                )
                if attached["disposition"] not in {"attached", "replay"}:
                    raise ControlRepositoryError("idempotency_conflict")
                completed = await connection.fetchrow(
                    """
                    SELECT * FROM complete_arena_api_idempotency(
                        $1, $2, $3, $4, 'arena_agent', $5, 201
                    )
                    """,
                    agent.owner_user_id,
                    _AGENT_ROUTE,
                    idempotency_key_digest,
                    request_hash,
                    agent.agent_id,
                )
                if completed["disposition"] not in {
                    "completed",
                    "replay",
                }:
                    raise ControlRepositoryError("idempotency_conflict")
                return HostedAgentCreation(
                    disposition=ReservationDisposition.CREATED,
                    agent=agent,
                )

    async def get_hosted_agent_update_replay(
        self,
        *,
        owner_user_id: str,
        idempotency_key_digest: str,
        request_hash: str,
    ) -> HostedAgentRecord | None:
        async with self._connection() as connection:
            replay = await connection.fetchrow(
                """
                SELECT * FROM lookup_completed_arena_api_idempotency(
                    $1, $2, $3, $4
                )
                """,
                owner_user_id,
                _AGENT_UPDATE_ROUTE,
                idempotency_key_digest,
                request_hash,
            )
            if replay["disposition"] == "conflict":
                raise ControlRepositoryError("idempotency_conflict")
            if replay["disposition"] != "replay":
                return None
            row = await connection.fetchrow(
                _AGENT_SELECT
                + " WHERE a.owner_user_id = $1 AND a.agent_id = $2",
                owner_user_id,
                replay["resource_id"],
            )
        if row is None:
            raise ControlRepositoryError("idempotency_conflict")
        return _agent(row)

    async def update_hosted_agent(
        self,
        *,
        agent: HostedAgentRecord,
        expected_config_hash: str,
        idempotency_key_digest: str,
        request_hash: str,
    ) -> HostedAgentRecord:
        async with self._connection() as connection:
            async with connection.transaction():
                reservation = await connection.fetchrow(
                    """
                    SELECT * FROM reserve_arena_api_idempotency(
                        $1, $2, $3, $4, $5
                    )
                    """,
                    agent.owner_user_id,
                    _AGENT_UPDATE_ROUTE,
                    idempotency_key_digest,
                    request_hash,
                    _IDEMPOTENCY_TTL_SECONDS,
                )
                disposition = reservation["disposition"]
                if disposition == "conflict":
                    raise ControlRepositoryError("idempotency_conflict")
                if disposition in {"replay", "retry", "in_progress"}:
                    resource_id = reservation["resource_id"]
                    if not resource_id:
                        raise ControlRepositoryError("idempotency_conflict")
                    row = await connection.fetchrow(
                        _AGENT_SELECT
                        + " WHERE a.owner_user_id = $1 AND a.agent_id = $2",
                        agent.owner_user_id,
                        resource_id,
                    )
                    if row is None:
                        raise ControlRepositoryError("idempotency_conflict")
                    return _agent(row)
                if disposition != "reserved":
                    raise ControlRepositoryError("idempotency_conflict")

                current = await connection.fetchrow(
                    """
                    SELECT
                        a.status AS agent_status,
                        a.runtime_update_job_id,
                        c.hosted_config_id,
                        c.credential_id,
                        c.provider,
                        c.config_hash,
                        c.status AS config_status,
                        b.runtime_binding_id,
                        b.route_status
                    FROM arena_agents AS a
                    JOIN arena_hosted_configs AS c
                      ON c.agent_id = a.agent_id
                    JOIN arena_runtime_bindings AS b
                      ON b.agent_id = a.agent_id
                     AND b.hosted_config_id = c.hosted_config_id
                     AND b.disabled_at IS NULL
                    WHERE a.owner_user_id = $1
                      AND a.agent_id = $2
                    FOR UPDATE OF a
                    """,
                    agent.owner_user_id,
                    agent.agent_id,
                )
                if current is None:
                    raise ControlRepositoryError("agent_not_found")
                if (
                    current["agent_status"] != "active"
                    or current["runtime_update_job_id"] is not None
                    or current["config_status"] != "ready"
                    or current["route_status"] != "ready"
                    or current["config_hash"] != expected_config_hash
                ):
                    raise ControlRepositoryError("agent_not_ready")
                if (
                    current["hosted_config_id"] != agent.hosted_config_id
                    or current["runtime_binding_id"]
                    != agent.runtime_binding_id
                    or current["credential_id"] != agent.credential_id
                ):
                    raise ControlRepositoryError("agent_not_ready")
                if current["provider"] != agent.provider_id:
                    raise ControlRepositoryError("provider_mismatch")

                active_game = await connection.fetchval(
                    """
                    SELECT 1
                    FROM game_agents
                    WHERE agent_id = $1
                      AND status IN ('joined', 'active', 'settling')
                    LIMIT 1
                    """,
                    agent.agent_id,
                )
                if active_game is not None:
                    raise ControlRepositoryError("agent_not_ready")

                credential = await connection.fetchrow(
                    """
                    SELECT credential_id, owner_user_id, provider, status
                    FROM arena_model_credentials
                    WHERE credential_id = $1
                      AND owner_user_id = $2
                    FOR UPDATE
                    """,
                    agent.credential_id,
                    agent.owner_user_id,
                )
                if credential is None:
                    raise ControlRepositoryError("credential_not_found")
                if credential["provider"] != agent.provider_id:
                    raise ControlRepositoryError("provider_mismatch")
                if credential["status"] != "valid":
                    raise ControlRepositoryError("credential_not_usable")

                candidate = {
                    "credential_id": agent.credential_id,
                    "provider": agent.provider_id,
                    "model": agent.model_id,
                    "thinking_enabled": agent.thinking_enabled,
                    "strategy_instructions": agent.strategy_instructions,
                    "prompt_version": agent.prompt_version,
                    "task_schema_version": agent.task_schema_version,
                    "action_schema_version": agent.action_schema_version,
                    "capability_version": agent.capability_version,
                    "adapter_version": agent.adapter_version,
                    "max_input_bytes": agent.max_input_bytes,
                    "max_context_items": agent.max_context_items,
                    "max_output_tokens": agent.max_output_tokens,
                }
                await connection.execute(
                    """
                    INSERT INTO hosted_credential_validation_jobs (
                        validation_job_id, agent_id, credential_id,
                        hosted_config_id, job_kind, candidate_config_snapshot,
                        candidate_config_hash, expected_current_config_hash,
                        validation_schema_version, status, max_attempts,
                        next_attempt_at, deadline_at, created_at
                    )
                    VALUES (
                        $1, $2, $3, $4, 'update', $5::jsonb, $6, $7,
                        'arena.credential-validation.v1', 'queued', 3,
                        $8::timestamptz,
                        $8::timestamptz + make_interval(mins => $9::integer),
                        $8::timestamptz
                    )
                    """,
                    agent.validation_job_id,
                    agent.agent_id,
                    agent.credential_id,
                    agent.hosted_config_id,
                    json.dumps(candidate, separators=(",", ":")),
                    agent.config_hash,
                    expected_config_hash,
                    agent.updated_at,
                    _VALIDATION_DEADLINE_MINUTES,
                )
                await connection.execute(
                    """
                    UPDATE arena_agents
                    SET runtime_update_job_id = $1,
                        updated_at = $2
                    WHERE agent_id = $3
                      AND runtime_update_job_id IS NULL
                    """,
                    agent.validation_job_id,
                    agent.updated_at,
                    agent.agent_id,
                )
                await connection.execute(
                    """
                    UPDATE arena_model_credentials
                    SET status = 'pending_validation',
                        updated_at = $1
                    WHERE credential_id = $2
                      AND status = 'valid'
                    """,
                    agent.updated_at,
                    agent.credential_id,
                )
                attached = await connection.fetchrow(
                    """
                    SELECT * FROM attach_arena_api_idempotency_resource(
                        $1, $2, $3, $4, 'arena_agent', $5
                    )
                    """,
                    agent.owner_user_id,
                    _AGENT_UPDATE_ROUTE,
                    idempotency_key_digest,
                    request_hash,
                    agent.agent_id,
                )
                if attached["disposition"] not in {"attached", "replay"}:
                    raise ControlRepositoryError("idempotency_conflict")
                completed = await connection.fetchrow(
                    """
                    SELECT * FROM complete_arena_api_idempotency(
                        $1, $2, $3, $4, 'arena_agent', $5, 202
                    )
                    """,
                    agent.owner_user_id,
                    _AGENT_UPDATE_ROUTE,
                    idempotency_key_digest,
                    request_hash,
                    agent.agent_id,
                )
                if completed["disposition"] not in {"completed", "replay"}:
                    raise ControlRepositoryError("idempotency_conflict")
                row = await connection.fetchrow(
                    _AGENT_SELECT
                    + " WHERE a.owner_user_id = $1 AND a.agent_id = $2",
                    agent.owner_user_id,
                    agent.agent_id,
                )
                if row is None:
                    raise ControlRepositoryError("agent_not_found")
                return _agent(row)

    async def get_hosted_agent_for_owner(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
    ) -> HostedAgentRecord | None:
        async with self._connection() as connection:
            row = await connection.fetchrow(
                _AGENT_SELECT
                + " WHERE a.owner_user_id = $1 AND a.agent_id = $2",
                owner_user_id,
                agent_id,
            )
        return _agent(row) if row is not None else None

    async def list_hosted_agents_for_owner(
        self,
        *,
        owner_user_id: str,
    ) -> tuple[HostedAgentRecord, ...]:
        async with self._connection() as connection:
            rows = await connection.fetch(
                _AGENT_SELECT
                + """
                  WHERE a.owner_user_id = $1
                  ORDER BY a.created_at DESC, a.agent_id DESC
                  LIMIT 100
                """,
                owner_user_id,
            )
        return tuple(_agent(row) for row in rows)


__all__ = ["PostgresHostedAgentControlRepository"]
