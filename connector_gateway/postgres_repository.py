"""PostgreSQL persistence for production Connector identity and runtime state."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from db_pool_config import api_pool_max_size

from .repository import DuplicateIdentityError, InvalidInviteError


def _record(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _timestamp(value: Any) -> datetime:
    """Convert the gateway's JSON-safe ISO timestamps for asyncpg."""

    if isinstance(value, datetime):
        resolved = value
    elif isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            resolved = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"Invalid Connector timestamp: {value!r}") from exc
    else:
        raise TypeError(
            "Connector timestamps must be datetime instances or ISO-8601 strings"
        )
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved


def _optional_timestamp(value: Any) -> datetime | None:
    return None if value is None else _timestamp(value)


class PostgresConnectorRepository:
    """Single-writer repository for a one-worker Gateway deployment.

    WebSocket ownership remains process-local. Durable entities are upserted in
    one transaction, so a restart can reconstruct device, runtime, binding,
    command, event and audit state without persisting socket objects.
    """

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._pool: Any = None
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        async with self._initialize_lock:
            if self._pool is not None:
                return
            try:
                import asyncpg  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise RuntimeError(
                    "asyncpg is required for PostgreSQL Connector persistence"
                ) from exc
            self._pool = await asyncpg.create_pool(
                self.database_url,
                min_size=0,
                max_size=api_pool_max_size(),
                command_timeout=30,
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("Connector repository is not initialized")
        return self._pool

    async def seed_invite(
        self, token_hash: str, expires_at: datetime | None = None
    ) -> None:
        pool = self._require_pool()
        await pool.execute(
            """
            INSERT INTO connector_invites (token_hash, expires_at)
            VALUES ($1, $2)
            ON CONFLICT (token_hash) DO NOTHING
            """,
            token_hash,
            expires_at,
        )

    async def is_invite_available(self, token_hash: str, checked_at: datetime) -> bool:
        pool = self._require_pool()
        return bool(
            await pool.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM connector_invites
                    WHERE token_hash = $1
                      AND consumed_at IS NULL
                      AND (expires_at IS NULL OR expires_at > $2)
                )
                """,
                token_hash,
                checked_at,
            )
        )

    async def consume_invite_and_create_user(
        self,
        token_hash: str,
        user_id: str,
        username: str,
        password_hash: str | None,
        temporary: bool,
        created_at: datetime,
    ) -> dict[str, Any]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                invite = await connection.fetchrow(
                    """
                    SELECT token_hash
                    FROM connector_invites
                    WHERE token_hash = $1
                      AND consumed_at IS NULL
                      AND (expires_at IS NULL OR expires_at > $2)
                    FOR UPDATE
                    """,
                    token_hash,
                    created_at,
                )
                if invite is None:
                    raise InvalidInviteError
                try:
                    user = await connection.fetchrow(
                        """
                        INSERT INTO connector_users (
                            user_id, username, password_hash, temporary, created_at
                        )
                        VALUES ($1, $2, $3, $4, $5)
                        RETURNING user_id, username, password_hash, temporary,
                                  identity_provider, provider_subject,
                                  created_at, disabled_at
                        """,
                        user_id,
                        username,
                        password_hash,
                        temporary,
                        created_at,
                    )
                except Exception as exc:
                    if getattr(exc, "sqlstate", None) == "23505":
                        raise DuplicateIdentityError from exc
                    raise
                updated = await connection.execute(
                    """
                    UPDATE connector_invites
                    SET consumed_at = $2, consumed_by = $3
                    WHERE token_hash = $1 AND consumed_at IS NULL
                    """,
                    token_hash,
                    created_at,
                    user_id,
                )
                if updated != "UPDATE 1":
                    raise InvalidInviteError
                return dict(user)

    async def create_password_user(
        self,
        user_id: str,
        username: str,
        password_hash: str,
        created_at: datetime,
    ) -> dict[str, Any]:
        try:
            row = await self._require_pool().fetchrow(
                """
                INSERT INTO connector_users (
                    user_id, username, password_hash, temporary,
                    identity_provider, provider_subject, created_at
                )
                VALUES ($1, $2, $3, FALSE, 'password', NULL, $4)
                RETURNING user_id, username, password_hash, temporary,
                          identity_provider, provider_subject,
                          created_at, disabled_at
                """,
                user_id,
                username,
                password_hash,
                created_at,
            )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise DuplicateIdentityError from exc
            raise
        return dict(row)

    async def get_user_by_username(
        self, normalized_username: str
    ) -> dict[str, Any] | None:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            SELECT user_id, username, password_hash, temporary,
                   identity_provider, provider_subject, created_at, disabled_at
            FROM connector_users
            WHERE username = $1
            """,
            normalized_username,
        )
        return dict(row) if row else None

    async def get_or_create_oauth_user(
        self,
        provider: str,
        subject: str,
        preferred_username: str,
        created_at: datetime,
    ) -> dict[str, Any]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                existing = await connection.fetchrow(
                    """
                    SELECT user_id, username, password_hash, temporary,
                           identity_provider, provider_subject,
                           created_at, disabled_at
                    FROM connector_users
                    WHERE identity_provider = $1 AND provider_subject = $2
                    FOR UPDATE
                    """,
                    provider,
                    subject,
                )
                if existing is not None:
                    return dict(existing)

                user_id = f"user_{uuid.uuid4().hex[:20]}"
                candidates = (
                    preferred_username,
                    f"{provider}-{subject}"[:64],
                )
                for username in dict.fromkeys(candidates):
                    created = await connection.fetchrow(
                        """
                        INSERT INTO connector_users (
                            user_id, username, password_hash, temporary,
                            identity_provider, provider_subject, created_at
                        )
                        VALUES ($1, $2, NULL, FALSE, $3, $4, $5)
                        ON CONFLICT DO NOTHING
                        RETURNING user_id, username, password_hash, temporary,
                                  identity_provider, provider_subject,
                                  created_at, disabled_at
                        """,
                        user_id,
                        username,
                        provider,
                        subject,
                        created_at,
                    )
                    if created is not None:
                        return dict(created)
                    existing = await connection.fetchrow(
                        """
                        SELECT user_id, username, password_hash, temporary,
                               identity_provider, provider_subject,
                               created_at, disabled_at
                        FROM connector_users
                        WHERE identity_provider = $1 AND provider_subject = $2
                        """,
                        provider,
                        subject,
                    )
                    if existing is not None:
                        return dict(existing)
                raise DuplicateIdentityError

    async def create_session(
        self,
        token_hash: str,
        user_id: str,
        csrf_hash: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        pool = self._require_pool()
        await pool.execute(
            """
            INSERT INTO connector_sessions (
                token_hash, user_id, csrf_hash, created_at, expires_at
            )
            VALUES ($1, $2, $3, $4, $5)
            """,
            token_hash,
            user_id,
            csrf_hash,
            created_at,
            expires_at,
        )

    async def get_session(self, token_hash: str) -> dict[str, Any] | None:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            SELECT s.token_hash, s.user_id, s.csrf_hash, s.created_at,
                   s.expires_at, s.revoked_at, u.username, u.password_hash,
                   u.temporary, u.identity_provider, u.provider_subject,
                   u.disabled_at
            FROM connector_sessions s
            JOIN connector_users u ON u.user_id = s.user_id
            WHERE s.token_hash = $1
            """,
            token_hash,
        )
        return dict(row) if row else None

    async def revoke_session(self, token_hash: str, revoked_at: datetime) -> None:
        pool = self._require_pool()
        await pool.execute(
            """
            UPDATE connector_sessions
            SET revoked_at = COALESCE(revoked_at, $2)
            WHERE token_hash = $1
            """,
            token_hash,
            revoked_at,
        )

    async def load_gateway_state(self) -> dict[str, Any]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            tables = (
                ("pairings", "connector_pairings", "created_at"),
                ("devices", "connector_devices", "created_at"),
                ("bindings", "connector_bindings", "created_at"),
                ("commands", "connector_commands", "created_at"),
                (
                    "agent_task_results",
                    "connector_agent_task_results",
                    "received_at",
                ),
            )
            state: dict[str, Any] = {}
            for key, table, order_by in tables:
                rows = await connection.fetch(
                    f"SELECT record FROM {table} ORDER BY {order_by} ASC"
                )
                state[key] = [_record(row["record"]) for row in rows]
            for key, table, order_by in (
                ("events", "connector_events", "received_at"),
                ("audit", "connector_audit", "occurred_at"),
            ):
                rows = await connection.fetch(
                    f"""
                    SELECT record
                    FROM (
                        SELECT record, {order_by}
                        FROM {table}
                        ORDER BY {order_by} DESC
                        LIMIT 10000
                    ) retained
                    ORDER BY {order_by} ASC
                    """
                )
                state[key] = [_record(row["record"]) for row in rows]
            return state

    async def save_gateway_state(self, state: dict[str, Any]) -> None:
        pool = self._require_pool()
        replace_runtime_device_ids = {
            str(value)
            for value in state.get("_replace_runtime_device_ids", [])
        }
        if not state.get("_incremental", False):
            replace_runtime_device_ids.update(
                str(device["device_id"])
                for device in state.get("devices", [])
            )
        async with pool.acquire() as connection:
            async with connection.transaction():
                for pairing in state["pairings"]:
                    await connection.execute(
                        """
                        INSERT INTO connector_pairings (
                            pairing_id, user_code, owner_id, device_code_hash,
                            status, created_at, expires_at, record
                        )
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
                        ON CONFLICT (pairing_id) DO UPDATE SET
                            owner_id = EXCLUDED.owner_id,
                            device_code_hash = EXCLUDED.device_code_hash,
                            status = EXCLUDED.status,
                            expires_at = EXCLUDED.expires_at,
                            record = EXCLUDED.record
                        """,
                        pairing["pairing_id"],
                        pairing["user_code"],
                        pairing.get("owner_id"),
                        pairing.get("device_code_hash"),
                        pairing["status"],
                        _timestamp(pairing["created_at"]),
                        _timestamp(pairing["expires_at"]),
                        json.dumps(pairing),
                    )
                for device in state["devices"]:
                    await connection.execute(
                        """
                        INSERT INTO connector_devices (
                            device_id, owner_id, token_hash, status, created_at,
                            revoked_at, record
                        )
                        VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
                        ON CONFLICT (device_id) DO UPDATE SET
                            owner_id = EXCLUDED.owner_id,
                            token_hash = EXCLUDED.token_hash,
                            status = EXCLUDED.status,
                            revoked_at = EXCLUDED.revoked_at,
                            record = EXCLUDED.record
                        """,
                        device["device_id"],
                        device["owner_id"],
                        device["token_hash"],
                        device["status"],
                        _timestamp(device["created_at"]),
                        _optional_timestamp(device.get("revoked_at")),
                        json.dumps(device),
                    )
                    if device["device_id"] in replace_runtime_device_ids:
                        await connection.execute(
                            "DELETE FROM connector_runtimes WHERE device_id = $1",
                            device["device_id"],
                        )
                        for runtime in device.get("runtimes", []):
                            await connection.execute(
                                """
                                INSERT INTO connector_runtimes (
                                    device_id, runtime_id, kind, available, record
                                )
                                VALUES ($1,$2,$3,$4,$5::jsonb)
                                """,
                                device["device_id"],
                                runtime["runtime_id"],
                                runtime["kind"],
                                runtime.get("available", True),
                                json.dumps(runtime),
                            )
                for binding in state["bindings"]:
                    await connection.execute(
                        """
                        INSERT INTO connector_bindings (
                            binding_id, device_id, runtime_id, agent_id, status,
                            created_at, record
                        )
                        VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
                        ON CONFLICT (binding_id) DO UPDATE SET
                            status = EXCLUDED.status, record = EXCLUDED.record
                        """,
                        binding["binding_id"],
                        binding["device_id"],
                        binding["runtime_id"],
                        binding["agent_id"],
                        binding["status"],
                        _timestamp(binding["created_at"]),
                        json.dumps(binding),
                    )
                for command in state["commands"]:
                    await connection.execute(
                        """
                        INSERT INTO connector_commands (
                            command_id, binding_id, device_id, status, action,
                            idempotency_key, created_at, expires_at, record
                        )
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
                        ON CONFLICT (command_id) DO UPDATE SET
                            status = EXCLUDED.status, record = EXCLUDED.record
                        """,
                        command["command_id"],
                        command["binding_id"],
                        command["device_id"],
                        command["status"],
                        command["action"],
                        command["idempotency_key"],
                        _timestamp(command["created_at"]),
                        _timestamp(command["expires_at"]),
                        json.dumps(command),
                    )
                for result in state.get("agent_task_results", []):
                    await connection.execute(
                        """
                        INSERT INTO connector_agent_task_results (
                            task_id, result_id, binding_id, device_id,
                            command_id, binding_epoch, result_hash,
                            received_at, record
                        )
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
                        ON CONFLICT (task_id) DO NOTHING
                        """,
                        result["task_id"],
                        result["result_id"],
                        result["binding_id"],
                        result["device_id"],
                        result["command_id"],
                        result["binding_epoch"],
                        result["result_hash"],
                        _timestamp(result["received_at"]),
                        json.dumps(result),
                    )
                for event in state["events"]:
                    await connection.execute(
                        """
                        INSERT INTO connector_events (
                            event_id, device_id, binding_id, sequence,
                            event_type, received_at, record
                        )
                        VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
                        ON CONFLICT (event_id) DO NOTHING
                        """,
                        event["event_id"],
                        event["device_id"],
                        event["binding_id"],
                        event["sequence"],
                        event["event_type"],
                        _timestamp(event["received_at"]),
                        json.dumps(event),
                    )
                for item in state["audit"]:
                    await connection.execute(
                        """
                        INSERT INTO connector_audit (
                            audit_id, owner_id, action, actor, occurred_at, record
                        )
                        VALUES ($1,$2,$3,$4,$5,$6::jsonb)
                        ON CONFLICT (audit_id) DO NOTHING
                        """,
                        item["audit_id"],
                        item.get("owner_id"),
                        item["action"],
                        item["actor"],
                        _timestamp(item["occurred_at"]),
                        json.dumps(item),
                    )
                if (
                    not state.get("_incremental", False)
                    or "pairings"
                    in state.get("_replace_collections", [])
                ):
                    # Pairing codes are short-lived, unauthenticated ingress
                    # state. Purge only when this collection changed.
                    await connection.execute(
                        """
                        DELETE FROM connector_pairings
                        WHERE status IN ('pending', 'approved', 'expired')
                          AND expires_at <= now()
                        """
                    )
                # Match the in-memory observability window. Pruning only when
                # the corresponding stream appended avoids two table scans on
                # every heartbeat.
                if state["events"]:
                    await connection.execute(
                        """
                        DELETE FROM connector_events
                        WHERE event_id IN (
                            SELECT event_id
                            FROM connector_events
                            ORDER BY received_at DESC, event_id DESC
                            OFFSET 10000
                        )
                        """
                    )
                if state["audit"]:
                    await connection.execute(
                        """
                        DELETE FROM connector_audit
                        WHERE audit_id IN (
                            SELECT audit_id
                            FROM connector_audit
                            ORDER BY occurred_at DESC, audit_id DESC
                            OFFSET 10000
                        )
                        """
                    )
