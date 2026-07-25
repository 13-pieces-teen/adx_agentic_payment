"""Owner-scoped Agent-to-Game participation and frozen Runtime binding."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from .hashing import sha256_identifier
from .ingress_security import secure_config_snapshot


class ArenaParticipationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Arena participation failed ({code})")


@dataclass(frozen=True, slots=True)
class GameParticipation:
    game_agent_id: str
    game_id: str
    agent_id: str
    runtime_binding_id: str
    runtime_kind: str
    status: str
    config_hash: str


@dataclass(frozen=True, slots=True)
class LocalAgentRegistration:
    agent_id: str
    display_name: str
    runtime_binding_id: str
    connector_binding_id: str
    connector_binding_epoch: int
    route_status: str


class PostgresArenaParticipationRepository:
    def __init__(self, dsn: str, *, pool: object | None = None) -> None:
        if not dsn and pool is None:
            raise ValueError("Arena participation PostgreSQL DSN is required")
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
                "asyncpg is required for Arena participation"
            ) from exc

        async def initialize_connection(connection: Any) -> None:
            await connection.set_type_codec(
                "jsonb",
                schema="pg_catalog",
                encoder=json.dumps,
                decoder=json.loads,
            )
            await connection.execute("SET ROLE adx_arena_api")
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
            raise RuntimeError(
                "Arena participation repository is not initialized"
            )
        return self._pool

    async def join(
        self,
        *,
        owner_user_id: str,
        game_id: str,
        agent_id: str,
        key_digest: str,
        request_digest: str,
    ) -> GameParticipation:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                reservation = await connection.fetchrow(
                    """
                    SELECT * FROM reserve_arena_api_idempotency(
                        $1, 'game_participants.create', $2, $3, 3600
                    )
                    """,
                    owner_user_id,
                    key_digest,
                    request_digest,
                )
                if reservation["disposition"] == "conflict":
                    raise ArenaParticipationError("idempotency_conflict")
                if reservation["disposition"] in {
                    "replay",
                    "retry",
                    "in_progress",
                }:
                    resource_id = reservation["resource_id"]
                    if not resource_id:
                        raise ArenaParticipationError(
                            "idempotency_conflict"
                        )
                    existing = await self._get(
                        connection,
                        owner_user_id,
                        resource_id,
                    )
                    if existing is None:
                        raise ArenaParticipationError(
                            "idempotency_conflict"
                        )
                    return existing

                game = await connection.fetchrow(
                    """
                    SELECT
                        g.game_id,
                        g.status,
                        g.config_snapshot,
                        pawnhouse.phase AS pawnhouse_phase
                    FROM games AS g
                    JOIN arena402.games AS pawnhouse
                      ON pawnhouse.game_id = g.game_id
                    WHERE g.game_id = $1
                    """,
                    game_id,
                )
                if game is None:
                    raise ArenaParticipationError("game_not_found")
                if game["status"] != "open":
                    raise ArenaParticipationError("game_not_open")
                if game["pawnhouse_phase"] not in {
                    "registration",
                    "portfolio_setup",
                }:
                    raise ArenaParticipationError("game_not_open")

                # reserve_arena_api_idempotency holds an owner-scoped
                # transaction advisory lock. Different idempotency keys from
                # the same owner therefore cannot race this single-participant
                # check and the UNIQUE(game_id, user_id) backstop.
                existing_row = await connection.fetchrow(
                    """
                    SELECT
                        ga.game_agent_id,
                        ga.game_id,
                        ga.agent_id,
                        ga.runtime_binding_id,
                        b.runtime_kind,
                        ga.status,
                        ga.config_hash
                    FROM game_agents AS ga
                    JOIN arena_runtime_bindings AS b
                      ON b.runtime_binding_id = ga.runtime_binding_id
                    WHERE ga.game_id = $1 AND ga.user_id = $2
                    """,
                    game_id,
                    owner_user_id,
                )
                if existing_row is not None:
                    existing = self._participation(existing_row)
                    if existing.agent_id != agent_id:
                        raise ArenaParticipationError(
                            "user_already_joined"
                        )
                    await self._complete_idempotency(
                        connection,
                        owner_user_id=owner_user_id,
                        route_key="game_participants.create",
                        key_digest=key_digest,
                        request_digest=request_digest,
                        resource_kind="game_agent",
                        game_agent_id=existing.game_agent_id,
                    )
                    return existing

                runtime = await connection.fetchrow(
                    """
                    SELECT
                        a.agent_id,
                        b.runtime_binding_id,
                        b.runtime_kind,
                        b.route_status,
                        b.connector_binding_id,
                        b.connector_binding_epoch,
                        hc.credential_id,
                        hc.provider,
                        hc.model,
                        hc.thinking_enabled,
                        hc.strategy_instructions,
                        hc.prompt_version,
                        hc.task_schema_version,
                        hc.action_schema_version,
                        hc.capability_version,
                        hc.adapter_version,
                        hc.max_input_bytes,
                        hc.max_context_items,
                        hc.max_output_tokens,
                        hc.status AS hosted_status
                    FROM arena_agents AS a
                    JOIN arena_runtime_bindings AS b
                      ON b.agent_id = a.agent_id
                     AND b.disabled_at IS NULL
                    LEFT JOIN arena_hosted_configs AS hc
                      ON hc.hosted_config_id = b.hosted_config_id
                     AND hc.agent_id = a.agent_id
                    WHERE a.owner_user_id = $1
                      AND a.agent_id = $2
                      AND a.status = 'active'
                    FOR SHARE OF a, b
                    """,
                    owner_user_id,
                    agent_id,
                )
                if runtime is None:
                    raise ArenaParticipationError("agent_not_found")
                if runtime["route_status"] != "ready":
                    raise ArenaParticipationError("runtime_not_ready")
                if (
                    runtime["runtime_kind"] == "hosted"
                    and runtime["hosted_status"] != "ready"
                ):
                    raise ArenaParticipationError("runtime_not_ready")

                snapshot = secure_config_snapshot(
                    self._runtime_snapshot(runtime)
                )
                config_hash = sha256_identifier(snapshot)
                game_agent_id = f"gagent-{uuid.uuid4().hex}"
                game_config = game["config_snapshot"]
                if isinstance(game_config, str):
                    game_config = json.loads(game_config)
                if not isinstance(game_config, Mapping):
                    game_config = {}
                initial_cash = game_config.get("initial_cash_atomic", 0)
                initial_inventory = game_config.get("initial_inventory", {})
                if (
                    not isinstance(initial_cash, int)
                    or isinstance(initial_cash, bool)
                    or initial_cash < 0
                    or not isinstance(initial_inventory, Mapping)
                    or set(initial_inventory)
                    != {"grain", "iron", "warhorse", "gems"}
                    or any(
                        not isinstance(quantity, int)
                        or isinstance(quantity, bool)
                        or quantity < 0
                        for quantity in initial_inventory.values()
                    )
                ):
                    raise ArenaParticipationError("invalid_game_config")

                await connection.execute(
                    """
                    INSERT INTO game_agents (
                        game_agent_id, game_id, user_id, agent_id,
                        runtime_binding_id, config_snapshot, config_hash,
                        status, initial_cash_atomic, initial_inventory
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6::jsonb, $7,
                        'joined', $8, $9::jsonb
                    )
                    """,
                    game_agent_id,
                    game_id,
                    owner_user_id,
                    agent_id,
                    runtime["runtime_binding_id"],
                    snapshot,
                    config_hash,
                    initial_cash,
                    dict(initial_inventory),
                )
                await connection.execute(
                    """
                    INSERT INTO arena402.game_participants (
                        game_participant_id,
                        game_id,
                        user_id,
                        agent_id,
                        runtime_binding_id,
                        runtime_kind,
                        portfolio_locked_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, clock_timestamp()
                    )
                    """,
                    game_agent_id,
                    game_id,
                    owner_user_id,
                    agent_id,
                    runtime["runtime_binding_id"],
                    runtime["runtime_kind"],
                )
                await connection.execute(
                    """
                    INSERT INTO arena402.balances (
                        game_participant_id,
                        cash_atomic,
                        initial_cash_atomic
                    )
                    VALUES ($1, $2, $2)
                    """,
                    game_agent_id,
                    initial_cash,
                )
                for good_id in ("grain", "iron", "warhorse", "gems"):
                    quantity = int(initial_inventory[good_id])
                    await connection.execute(
                        """
                        INSERT INTO arena402.holdings (
                            game_participant_id,
                            game_id,
                            good_id,
                            quantity,
                            initial_quantity
                        )
                        VALUES ($1, $2, $3, $4, $4)
                        """,
                        game_agent_id,
                        game_id,
                        good_id,
                        quantity,
                    )
                await connection.execute(
                    """
                    UPDATE arena402.games
                    SET phase = 'portfolio_setup'
                    WHERE game_id = $1 AND phase = 'registration'
                    """,
                    game_id,
                )
                await connection.execute(
                    """
                    INSERT INTO arena402.game_events (
                        game_id,
                        event_type,
                        public_payload,
                        source_idempotency_key
                    )
                    VALUES (
                        $1,
                        'participant.joined',
                        jsonb_build_object(
                            'participantId', $2::TEXT,
                            'agentId', $3::TEXT,
                            'runtimeKind', $4::TEXT
                        ),
                        $1 || ':' || $2 || ':joined'
                    )
                    ON CONFLICT (game_id, source_idempotency_key)
                    DO NOTHING
                    """,
                    game_id,
                    game_agent_id,
                    agent_id,
                    runtime["runtime_kind"],
                )
                await self._complete_idempotency(
                    connection,
                    owner_user_id=owner_user_id,
                    route_key="game_participants.create",
                    key_digest=key_digest,
                    request_digest=request_digest,
                    resource_kind="game_agent",
                    game_agent_id=game_agent_id,
                )
                return GameParticipation(
                    game_agent_id=game_agent_id,
                    game_id=game_id,
                    agent_id=agent_id,
                    runtime_binding_id=runtime["runtime_binding_id"],
                    runtime_kind=runtime["runtime_kind"],
                    status="joined",
                    config_hash=config_hash,
                )

    async def register_local_agent(
        self,
        *,
        owner_user_id: str,
        connector_binding_id: str,
        display_name: str,
        key_digest: str,
        request_digest: str,
    ) -> LocalAgentRegistration:
        if not display_name.strip() or len(display_name) > 120:
            raise ArenaParticipationError("invalid_display_name")
        pool = self._require_pool()
        try:
            async with pool.acquire() as connection:
                async with connection.transaction():
                    reservation = await connection.fetchrow(
                        """
                        SELECT * FROM reserve_local_agent_idempotency(
                            $1, $2, $3, 3600
                        )
                        """,
                        owner_user_id,
                        key_digest,
                        request_digest,
                    )
                    if reservation["disposition"] == "conflict":
                        raise ArenaParticipationError(
                            "idempotency_conflict"
                        )
                    if reservation["disposition"] in {
                        "replay",
                        "retry",
                        "in_progress",
                    }:
                        resource_id = reservation["resource_id"]
                        if not resource_id:
                            raise ArenaParticipationError(
                                "idempotency_conflict"
                            )
                        existing = await self._get_local_agent(
                            connection,
                            owner_user_id,
                            str(resource_id),
                        )
                        if existing is None:
                            raise ArenaParticipationError(
                                "idempotency_conflict"
                            )
                        return existing

                    route = await connection.fetchrow(
                        """
                        SELECT *
                        FROM resolve_connector_binding_for_arena($1, $2)
                        """,
                        owner_user_id,
                        connector_binding_id,
                    )
                    if route is None:
                        raise ArenaParticipationError(
                            "connector_binding_not_found"
                        )
                    existing_row = await connection.fetchrow(
                        """
                        SELECT
                            a.agent_id,
                            a.name AS display_name,
                            b.runtime_binding_id,
                            b.connector_binding_id,
                            b.connector_binding_epoch,
                            b.route_status
                        FROM arena_agents AS a
                        JOIN arena_runtime_bindings AS b
                          ON b.agent_id = a.agent_id
                        WHERE a.owner_user_id = $1
                          AND b.runtime_kind = 'connector'
                          AND b.connector_binding_id = $2
                          AND b.connector_binding_epoch = $3
                          AND b.disabled_at IS NULL
                        """,
                        owner_user_id,
                        connector_binding_id,
                        route["binding_epoch"],
                    )
                    if existing_row is not None:
                        existing = self._local_agent(existing_row)
                        if existing.display_name != display_name:
                            raise ArenaParticipationError(
                                "connector_binding_already_registered"
                            )
                        await self._complete_local_agent_idempotency(
                            connection,
                            owner_user_id=owner_user_id,
                            key_digest=key_digest,
                            request_digest=request_digest,
                            agent_id=existing.agent_id,
                        )
                        return existing

                    agent_id = str(route["agent_id"])
                    runtime_binding_id = (
                        f"rbind-connector-{uuid.uuid4().hex}"
                    )
                    await connection.execute(
                        """
                        INSERT INTO arena_agents (
                            agent_id, owner_user_id, name, status
                        )
                        VALUES ($1, $2, $3, 'active')
                        """,
                        agent_id,
                        owner_user_id,
                        display_name,
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
                        VALUES ($1, $2, 'connector', $3, $4, 'ready')
                        """,
                        runtime_binding_id,
                        agent_id,
                        connector_binding_id,
                        route["binding_epoch"],
                    )
                    await self._complete_local_agent_idempotency(
                        connection,
                        owner_user_id=owner_user_id,
                        key_digest=key_digest,
                        request_digest=request_digest,
                        agent_id=agent_id,
                    )
                    return LocalAgentRegistration(
                        agent_id=agent_id,
                        display_name=display_name,
                        runtime_binding_id=runtime_binding_id,
                        connector_binding_id=connector_binding_id,
                        connector_binding_epoch=int(
                            route["binding_epoch"]
                        ),
                        route_status="ready",
                    )
        except ArenaParticipationError:
            raise
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise ArenaParticipationError(
                    "connector_binding_already_registered"
                ) from exc
            raise

    async def list_for_owner(
        self,
        owner_user_id: str,
    ) -> tuple[GameParticipation, ...]:
        rows = await self._require_pool().fetch(
            """
            SELECT
                ga.game_agent_id,
                ga.game_id,
                ga.agent_id,
                ga.runtime_binding_id,
                b.runtime_kind,
                ga.status,
                ga.config_hash
            FROM game_agents AS ga
            JOIN arena_runtime_bindings AS b
              ON b.runtime_binding_id = ga.runtime_binding_id
            WHERE ga.user_id = $1
            ORDER BY ga.joined_at DESC, ga.game_agent_id DESC
            LIMIT 100
            """,
            owner_user_id,
        )
        return tuple(self._participation(row) for row in rows)

    @staticmethod
    def _runtime_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
        runtime_kind = row["runtime_kind"]
        if runtime_kind == "hosted":
            return {
                "runtime_kind": "hosted",
                "credential_id": row["credential_id"],
                "provider_id": row["provider"],
                "model_id": row["model"],
                "thinking_enabled": row["thinking_enabled"],
                "strategy_instructions": row["strategy_instructions"],
                "prompt_version": row["prompt_version"],
                "task_schema_version": row["task_schema_version"],
                "action_schema_version": row["action_schema_version"],
                "capability_version": row["capability_version"],
                "adapter_version": row["adapter_version"],
                "max_input_bytes": row["max_input_bytes"],
                "max_context_items": row["max_context_items"],
                "max_output_tokens": row["max_output_tokens"],
            }
        if runtime_kind == "connector":
            return {
                "runtime_kind": "connector",
                "credential_id": None,
                "connector_binding_id": row["connector_binding_id"],
                "connector_binding_epoch": row[
                    "connector_binding_epoch"
                ],
            }
        return {
            "runtime_kind": runtime_kind,
            "credential_id": None,
            "connector_binding_id": row["connector_binding_id"],
            "connector_binding_epoch": row["connector_binding_epoch"],
            "task_schema_version": "arena.agent-task.v1",
            "action_schema_version": "arena.action.v1",
        }

    async def _get(
        self,
        connection: Any,
        owner_user_id: str,
        game_agent_id: str,
    ) -> GameParticipation | None:
        row = await connection.fetchrow(
            """
            SELECT
                ga.game_agent_id,
                ga.game_id,
                ga.agent_id,
                ga.runtime_binding_id,
                b.runtime_kind,
                ga.status,
                ga.config_hash
            FROM game_agents AS ga
            JOIN arena_runtime_bindings AS b
              ON b.runtime_binding_id = ga.runtime_binding_id
            WHERE ga.user_id = $1 AND ga.game_agent_id = $2
            """,
            owner_user_id,
            game_agent_id,
        )
        return None if row is None else self._participation(row)

    async def _get_local_agent(
        self,
        connection: Any,
        owner_user_id: str,
        agent_id: str,
    ) -> LocalAgentRegistration | None:
        row = await connection.fetchrow(
            """
            SELECT
                a.agent_id,
                a.name AS display_name,
                b.runtime_binding_id,
                b.connector_binding_id,
                b.connector_binding_epoch,
                b.route_status
            FROM arena_agents AS a
            JOIN arena_runtime_bindings AS b
              ON b.agent_id = a.agent_id
            WHERE a.owner_user_id = $1
              AND a.agent_id = $2
              AND b.runtime_kind = 'connector'
              AND b.disabled_at IS NULL
            """,
            owner_user_id,
            agent_id,
        )
        return None if row is None else self._local_agent(row)

    @staticmethod
    async def _complete_idempotency(
        connection: Any,
        *,
        owner_user_id: str,
        route_key: str,
        key_digest: str,
        request_digest: str,
        resource_kind: str,
        game_agent_id: str,
    ) -> None:
        attached = await connection.fetchrow(
            """
            SELECT * FROM attach_arena_api_idempotency_resource(
                $1, $2, $3, $4, $5, $6
            )
            """,
            owner_user_id,
            route_key,
            key_digest,
            request_digest,
            resource_kind,
            game_agent_id,
        )
        if attached["disposition"] not in {"attached", "replay"}:
            raise ArenaParticipationError("idempotency_conflict")
        completed = await connection.fetchrow(
            """
            SELECT * FROM complete_arena_api_idempotency(
                $1, $2, $3, $4, $5, $6, 201
            )
            """,
            owner_user_id,
            route_key,
            key_digest,
            request_digest,
            resource_kind,
            game_agent_id,
        )
        if completed["disposition"] not in {"completed", "replay"}:
            raise ArenaParticipationError("idempotency_conflict")

    @staticmethod
    async def _complete_local_agent_idempotency(
        connection: Any,
        *,
        owner_user_id: str,
        key_digest: str,
        request_digest: str,
        agent_id: str,
    ) -> None:
        completed = await connection.fetchrow(
            """
            SELECT * FROM complete_local_agent_idempotency(
                $1, $2, $3, $4
            )
            """,
            owner_user_id,
            key_digest,
            request_digest,
            agent_id,
        )
        if completed["disposition"] not in {"completed", "replay"}:
            raise ArenaParticipationError("idempotency_conflict")

    @staticmethod
    def _participation(row: Mapping[str, Any]) -> GameParticipation:
        return GameParticipation(
            game_agent_id=row["game_agent_id"],
            game_id=row["game_id"],
            agent_id=row["agent_id"],
            runtime_binding_id=row["runtime_binding_id"],
            runtime_kind=row["runtime_kind"],
            status=row["status"],
            config_hash=row["config_hash"],
        )

    @staticmethod
    def _local_agent(
        row: Mapping[str, Any],
    ) -> LocalAgentRegistration:
        return LocalAgentRegistration(
            agent_id=row["agent_id"],
            display_name=row["display_name"],
            runtime_binding_id=row["runtime_binding_id"],
            connector_binding_id=row["connector_binding_id"],
            connector_binding_epoch=int(
                row["connector_binding_epoch"]
            ),
            route_status=row["route_status"],
        )


__all__ = [
    "ArenaParticipationError",
    "GameParticipation",
    "LocalAgentRegistration",
    "PostgresArenaParticipationRepository",
]
