"""Connector Binding to Arena Agent/Runtime registration bridge."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from db_pool_config import api_pool_max_size


class ConnectorRegistrationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ConnectorAgentRegistration:
    agent_id: str
    runtime_binding_id: str
    route_status: str

    def public(self) -> dict[str, str]:
        return {
            "agentId": self.agent_id,
            "runtimeBindingId": self.runtime_binding_id,
            "runtimeKind": "connector",
            "routeStatus": self.route_status,
            "schemaVersion": "arena.connector-registration.v1",
        }


class PostgresConnectorArenaRegistrar:
    def __init__(self, dsn: str, *, pool: Any | None = None) -> None:
        if not dsn and pool is None:
            raise ValueError("Connector Arena registration DSN is required")
        self._dsn = dsn
        self._pool = pool
        self._owns_pool = pool is None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        try:
            import asyncpg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "asyncpg is required for Connector Arena registration"
            ) from exc
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=0,
            max_size=api_pool_max_size(),
            command_timeout=30,
        )

    async def close(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
        self._pool = None

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError(
                "Connector Arena registrar is not initialized"
            )
        return self._pool

    async def register_connector_binding(
        self,
        *,
        owner_user_id: str,
        connector_binding_id: str,
    ) -> dict[str, str]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                binding = await connection.fetchrow(
                    """
                    SELECT
                        cb.binding_id,
                        cb.agent_id,
                        cb.status AS binding_status,
                        cb.record AS binding_record,
                        cd.owner_id,
                        cd.revoked_at,
                        cr.available,
                        cr.record AS runtime_record
                    FROM connector_bindings AS cb
                    JOIN connector_devices AS cd
                      ON cd.device_id = cb.device_id
                    JOIN connector_runtimes AS cr
                      ON cr.device_id = cb.device_id
                     AND cr.runtime_id = cb.runtime_id
                    WHERE cb.binding_id = $1
                    FOR SHARE OF cb, cd, cr
                    """,
                    connector_binding_id,
                )
                if binding is None or binding["owner_id"] != owner_user_id:
                    raise ConnectorRegistrationError(
                        "connector_binding_not_found"
                    )

                binding_record = self._mapping(binding["binding_record"])
                runtime_record = self._mapping(binding["runtime_record"])
                binding_epoch = binding_record.get("binding_epoch")
                if (
                    not isinstance(binding_epoch, int)
                    or isinstance(binding_epoch, bool)
                    or binding_epoch < 1
                ):
                    raise ConnectorRegistrationError(
                        "invalid_connector_binding_epoch"
                    )

                capabilities = runtime_record.get("capabilities", [])
                capability_set = (
                    {str(value) for value in capabilities}
                    if isinstance(capabilities, list)
                    else set()
                )
                route_status = (
                    "ready"
                    if (
                        binding["revoked_at"] is None
                        and bool(binding["available"])
                        and binding["binding_status"]
                        in {"available", "running"}
                        and {
                            "session.start",
                            "task.dispatch",
                        }.issubset(capability_set)
                    )
                    else "provisioning"
                )
                agent_id = str(binding["agent_id"])
                display_name = str(
                    binding_record.get("display_name")
                    or runtime_record.get("display_name")
                    or "Local Agent"
                )[:120]
                existing_owner = await connection.fetchval(
                    """
                    SELECT owner_user_id
                    FROM arena_agents
                    WHERE agent_id = $1
                    """,
                    agent_id,
                )
                if (
                    existing_owner is not None
                    and existing_owner != owner_user_id
                ):
                    raise ConnectorRegistrationError(
                        "agent_identity_conflict"
                    )
                persisted_owner = await connection.fetchval(
                    """
                    INSERT INTO arena_agents (
                        agent_id, owner_user_id, name, status
                    )
                    VALUES ($1, $2, $3, 'active')
                    ON CONFLICT (agent_id) DO UPDATE
                    SET name = EXCLUDED.name,
                        updated_at = clock_timestamp()
                    WHERE arena_agents.owner_user_id = EXCLUDED.owner_user_id
                    RETURNING owner_user_id
                    """,
                    agent_id,
                    owner_user_id,
                    display_name,
                )
                if persisted_owner != owner_user_id:
                    raise ConnectorRegistrationError(
                        "agent_identity_conflict"
                    )

                runtime_binding_id = f"rbind:connector:{connector_binding_id}"
                active_route = await connection.fetchrow(
                    """
                    SELECT runtime_binding_id, connector_binding_id
                    FROM arena_runtime_bindings
                    WHERE agent_id = $1 AND disabled_at IS NULL
                    """,
                    agent_id,
                )
                if (
                    active_route is not None
                    and active_route["connector_binding_id"]
                    != connector_binding_id
                ):
                    raise ConnectorRegistrationError(
                        "agent_active_route_conflict"
                    )
                await connection.execute(
                    """
                    INSERT INTO arena_runtime_bindings (
                        runtime_binding_id,
                        agent_id,
                        runtime_kind,
                        connector_binding_id,
                        connector_binding_epoch,
                        route_status
                    )
                    VALUES ($1, $2, 'connector', $3, $4, $5)
                    ON CONFLICT (runtime_binding_id) DO UPDATE
                    SET connector_binding_epoch = EXCLUDED.connector_binding_epoch,
                        route_status = EXCLUDED.route_status,
                        updated_at = clock_timestamp()
                    """,
                    runtime_binding_id,
                    agent_id,
                    connector_binding_id,
                    binding_epoch,
                    route_status,
                )
                return ConnectorAgentRegistration(
                    agent_id=agent_id,
                    runtime_binding_id=runtime_binding_id,
                    route_status=route_status,
                ).public()

    @staticmethod
    def _mapping(value: object) -> Mapping[str, object]:
        if isinstance(value, str):
            parsed = json.loads(value)
            return parsed if isinstance(parsed, Mapping) else {}
        return value if isinstance(value, Mapping) else {}


__all__ = [
    "ConnectorAgentRegistration",
    "ConnectorRegistrationError",
    "PostgresConnectorArenaRegistrar",
]
