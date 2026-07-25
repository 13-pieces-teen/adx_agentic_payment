"""PostgreSQL repository for the clean-slate King's Pawnhouse vertical slice."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from arena_core.hashing import sha256_identifier, sha256_text_identifier

from .events import (
    EffectKind,
    EventEffect,
    WorldEvent,
    WorldState,
    schedule_commitment,
)
from .goods import GOODS, GOOD_IDS, require_good
from .market import Pairing, PoolEntry, fcfs_pair
from .money import gold
from .negotiation import Negotiation, NegotiationAction, NegotiationStatus
from .portfolio import Portfolio
from .ranking import calculate_rankings
from .rule_runtime import RuleRuntime, RuleStrategy
from .settlement import (
    ChainConfirmation,
    SettlementAccount,
    SettlementConfig,
    SettlementError,
    SettlementIntent,
    normalize_authorization_nonce,
    normalize_tx_hash,
    validate_chain_confirmation,
)


class PawnhouseRepositoryError(RuntimeError):
    pass


_INTENT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class PostgresPawnhouseRepository:
    def __init__(self, database_url: str, *, pool: Any | None = None) -> None:
        self.database_url = database_url
        self._pool = pool
        self._owns_pool = pool is None
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        async with self._initialize_lock:
            if self._pool is not None:
                return
            import asyncpg

            self._pool = await asyncpg.create_pool(
                self.database_url,
                min_size=1,
                max_size=5,
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
        await connection.execute("SET ROLE adx_arena_core")
        await connection.execute("SET search_path TO pg_catalog, arena402, public")

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("Pawnhouse repository is not initialized")
        return self._pool

    async def create_game(
        self,
        *,
        game_id: str,
        events: tuple[WorldEvent, ...],
        event_seed: str,
        event_deck_id: str = "pawnhouse-standard-v1",
        event_mode: str = "fixed_demo",
        action_timeout_ms: int = 90_000,
        max_negotiation_turns: int = 3,
        max_participants: int = 16,
        settlement_config: SettlementConfig | None = None,
        operator_user_id: str | None = None,
    ) -> dict[str, object]:
        if not game_id:
            raise PawnhouseRepositoryError("game_id_required")
        if max_participants < 2 or max_participants > 64:
            raise PawnhouseRepositoryError("invalid_max_participants")
        commitment = schedule_commitment(events, seed=event_seed)
        resolved_settlement = settlement_config or SettlementConfig()
        config = {
            "world": "aurelia-402",
            "venue": "kings-pawnhouse",
            "roundCount": len(events),
            "maxParticipants": max_participants,
            "eventDeckId": event_deck_id,
            "eventDeckVersion": 1,
            "eventMode": event_mode,
            "initialNetWorthAtomic": "20000000",
            "initial_cash_atomic": 20_000_000,
            "initial_inventory": {
                "grain": 0,
                "iron": 0,
                "warhorse": 0,
                "gems": 0,
            },
            "fixedTradeQuantity": 1,
            "goldScale": 1_000_000,
            "settlement": resolved_settlement.to_snapshot(),
            "schemaVersion": "arena.pawnhouse-game.v1",
        }
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                inserted = await connection.fetchval(
                    """
                    INSERT INTO arena402.games (
                        game_id, round_count, action_timeout_ms,
                        max_negotiation_turns, max_participants,
                        config_snapshot, event_seed, event_schedule_commitment,
                        operator_user_id
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9
                    )
                    ON CONFLICT (game_id) DO NOTHING
                    RETURNING game_id
                    """,
                    game_id,
                    len(events),
                    action_timeout_ms,
                    max_negotiation_turns,
                    max_participants,
                    _json(config),
                    event_seed,
                    commitment,
                    operator_user_id,
                )
                if inserted is None:
                    raise PawnhouseRepositoryError("game_already_exists")
                await connection.execute(
                    """
                    INSERT INTO public.games (
                        game_id, status, action_timeout_ms, config_snapshot
                    )
                    VALUES ($1, 'open', $2, $3::jsonb)
                    """,
                    game_id,
                    action_timeout_ms,
                    _json(config),
                )
                await connection.execute(
                    """
                    INSERT INTO arena402.current_game (
                        singleton,
                        game_id,
                        start_threshold,
                        max_participants
                    )
                    SELECT
                        TRUE,
                        game_id,
                        min_participants,
                        max_participants
                    FROM arena402.games
                    WHERE game_id = $1
                      AND min_participants BETWEEN 2 AND 12
                      AND max_participants BETWEEN min_participants AND 12
                    ON CONFLICT (singleton) DO NOTHING
                    """,
                    game_id,
                )
                for good in GOODS.values():
                    await connection.execute(
                        """
                        INSERT INTO arena402.game_goods (
                            game_id, good_id, display_name,
                            initial_price_atomic
                        )
                        VALUES ($1, $2, $3, $4)
                        """,
                        game_id,
                        good.good_id,
                        good.display_name,
                        good.initial_price_atomic,
                    )
                for event in events:
                    await connection.execute(
                        """
                        INSERT INTO arena402.event_schedule (
                            game_id, round_index, event_id, display_name,
                            narrative, duration_rounds, effect_snapshot,
                            schema_version
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
                        """,
                        game_id,
                        event.reveal_round,
                        event.event_id,
                        event.display_name,
                        event.narrative,
                        event.duration_rounds,
                        _json([effect.to_wire() for effect in event.effects]),
                        event.schema_version,
                    )
                await self._event(
                    connection,
                    game_id=game_id,
                    event_type="game.created",
                    source_key=f"{game_id}:created",
                    public_payload={
                        "roundCount": len(events),
                        "eventScheduleCommitment": commitment,
                    },
                )
        return {
            "gameId": game_id,
            "phase": "registration",
            "eventScheduleCommitment": commitment,
        }

    async def list_games(self, *, limit: int = 50) -> list[dict[str, object]]:
        if limit < 1 or limit > 100:
            raise ValueError("game list limit must be between 1 and 100")
        rows = await self._require_pool().fetch(
            """
            SELECT
                g.game_id,
                g.phase,
                g.round_count,
                g.current_round,
                g.max_participants,
                g.created_at,
                count(p.game_participant_id) AS participant_count
            FROM arena402.games AS g
            LEFT JOIN arena402.game_participants AS p
              ON p.game_id = g.game_id
            GROUP BY
                g.game_id,
                g.phase,
                g.round_count,
                g.current_round,
                g.max_participants,
                g.created_at
            ORDER BY g.created_at DESC, g.game_id DESC
            LIMIT $1
            """,
            limit,
        )
        return [
            {
                "gameId": str(row["game_id"]),
                "phase": str(row["phase"]),
                "roundCount": int(row["round_count"]),
                "currentRound": int(row["current_round"]),
                "participantCount": int(row["participant_count"]),
                "maxParticipants": int(row["max_participants"]),
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    async def add_rule_participant(
        self,
        *,
        game_id: str,
        user_id: str,
        agent_id: str,
        portfolio: Portfolio,
        strategy: RuleStrategy,
    ) -> str:
        participant_id = f"gp:{game_id}:{agent_id}"
        # The binding is a game-join snapshot. The same logical Agent may join
        # later games without sharing mutable strategy/configuration state.
        runtime_binding_id = f"rule:{participant_id}"
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                phase = await connection.fetchrow(
                    """
                    SELECT phase, max_participants
                    FROM arena402.games
                    WHERE game_id = $1
                    FOR UPDATE
                    """,
                    game_id,
                )
                if phase is None:
                    raise PawnhouseRepositoryError("game_not_found")
                if phase["phase"] not in ("registration", "portfolio_setup"):
                    raise PawnhouseRepositoryError("game_not_joinable")
                participant_count = int(
                    await connection.fetchval(
                        """
                        SELECT count(*)
                        FROM arena402.game_participants
                        WHERE game_id = $1
                        """,
                        game_id,
                    )
                )
                if participant_count >= int(phase["max_participants"]):
                    raise PawnhouseRepositoryError(
                        "game_participant_limit_reached"
                    )
                await connection.execute(
                    """
                    INSERT INTO arena402.game_participants (
                        game_participant_id, game_id, user_id, agent_id,
                        runtime_binding_id, runtime_kind, portfolio_locked_at
                    )
                    VALUES ($1, $2, $3, $4, $5, 'rule', clock_timestamp())
                    """,
                    participant_id,
                    game_id,
                    user_id,
                    agent_id,
                    runtime_binding_id,
                )
                await connection.execute(
                    """
                    INSERT INTO arena402.balances (
                        game_participant_id, cash_atomic, initial_cash_atomic
                    )
                    VALUES ($1, $2, $2)
                    """,
                    participant_id,
                    portfolio.cash_atomic,
                )
                for good_id in GOOD_IDS:
                    quantity = portfolio.holdings[good_id]
                    await connection.execute(
                        """
                        INSERT INTO arena402.holdings (
                            game_participant_id, game_id, good_id, quantity,
                            initial_quantity
                        )
                        VALUES ($1, $2, $3, $4, $4)
                        """,
                        participant_id,
                        game_id,
                        good_id,
                        quantity,
                    )
                await connection.execute(
                    """
                    INSERT INTO arena402.rule_runtime_configs (
                        runtime_binding_id, game_participant_id, intent,
                        good_id, target_price_atomic, public_message
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    runtime_binding_id,
                    participant_id,
                    strategy.intent,
                    strategy.good,
                    strategy.target_price_atomic,
                    strategy.public_message,
                )
                await connection.execute(
                    """
                    UPDATE arena402.games
                    SET phase = 'portfolio_setup'
                    WHERE game_id = $1
                      AND phase = 'registration'
                    """,
                    game_id,
                )
                await self._event(
                    connection,
                    game_id=game_id,
                    event_type="participant.joined",
                    source_key=f"{game_id}:{participant_id}:joined",
                    public_payload={
                        "participantId": participant_id,
                        "agentId": agent_id,
                        "runtimeKind": "rule",
                    },
                )
        return participant_id

    async def add_hosted_participant(
        self,
        *,
        game_id: str,
        user_id: str,
        agent_id: str,
        portfolio: Portfolio,
        settlement_account: SettlementAccount | None = None,
    ) -> str:
        participant_id = f"gp:{game_id}:{agent_id}"
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                phase = await connection.fetchrow(
                    """
                    SELECT phase, config_snapshot, max_participants
                    FROM arena402.games
                    WHERE game_id = $1
                    FOR UPDATE
                    """,
                    game_id,
                )
                if phase is None:
                    raise PawnhouseRepositoryError("game_not_found")
                if phase["phase"] not in ("registration", "portfolio_setup"):
                    raise PawnhouseRepositoryError("game_not_joinable")
                participant_count = int(
                    await connection.fetchval(
                        """
                        SELECT count(*)
                        FROM arena402.game_participants
                        WHERE game_id = $1
                        """,
                        game_id,
                    )
                )
                if participant_count >= int(phase["max_participants"]):
                    raise PawnhouseRepositoryError(
                        "game_participant_limit_reached"
                    )
                game_config = (
                    json.loads(phase["config_snapshot"])
                    if isinstance(phase["config_snapshot"], str)
                    else dict(phase["config_snapshot"])
                )
                settlement_config = self._settlement_config(game_config)
                if settlement_config.authorization_mode != "none":
                    if settlement_account is None:
                        raise PawnhouseRepositoryError(
                            "settlement_account_required"
                        )
                    if settlement_account.chain_id != settlement_config.chain_id:
                        raise PawnhouseRepositoryError(
                            "settlement_account_chain_mismatch"
                        )
                hosted = await connection.fetchrow(
                    """
                    SELECT
                        a.agent_id,
                        b.runtime_binding_id,
                        hc.credential_id,
                        hc.provider,
                        hc.model,
                        hc.thinking_enabled,
                        hc.strategy_instructions,
                        hc.max_output_tokens,
                        hc.prompt_version,
                        hc.task_schema_version,
                        hc.action_schema_version,
                        hc.capability_version,
                        hc.adapter_version
                    FROM public.arena_agents AS a
                    JOIN public.arena_runtime_bindings AS b
                      ON b.agent_id = a.agent_id
                     AND b.runtime_kind = 'hosted'
                     AND b.disabled_at IS NULL
                    JOIN public.arena_hosted_configs AS hc
                      ON hc.hosted_config_id = b.hosted_config_id
                     AND hc.agent_id = a.agent_id
                    JOIN public.arena_model_credentials AS c
                      ON c.credential_id = hc.credential_id
                    WHERE a.agent_id = $1
                      AND a.owner_user_id = $2
                      AND a.status = 'active'
                      AND b.route_status = 'ready'
                      AND hc.status = 'ready'
                      AND c.status = 'valid'
                    """,
                    agent_id,
                    user_id,
                )
                if hosted is None:
                    raise PawnhouseRepositoryError("hosted_agent_not_ready")
                config_snapshot = {
                    "provider_id": hosted["provider"],
                    "model_id": hosted["model"],
                    "credential_id": hosted["credential_id"],
                    "thinking_enabled": hosted["thinking_enabled"],
                    "strategy_instructions": hosted["strategy_instructions"],
                    "max_output_tokens": hosted["max_output_tokens"],
                    "prompt_version": hosted["prompt_version"],
                    "task_schema_version": hosted["task_schema_version"],
                    "action_schema_version": hosted["action_schema_version"],
                    "capability_version": hosted["capability_version"],
                    "adapter_version": hosted["adapter_version"],
                }
                config_hash = sha256_identifier(config_snapshot)
                await connection.execute(
                    """
                    INSERT INTO arena402.game_participants (
                        game_participant_id, game_id, user_id, agent_id,
                        runtime_binding_id, runtime_kind, portfolio_locked_at
                    )
                    VALUES ($1, $2, $3, $4, $5, 'hosted', clock_timestamp())
                    """,
                    participant_id,
                    game_id,
                    user_id,
                    agent_id,
                    hosted["runtime_binding_id"],
                )
                await connection.execute(
                    """
                    INSERT INTO public.game_agents (
                        game_agent_id, game_id, user_id, agent_id,
                        runtime_binding_id, config_snapshot, config_hash,
                        initial_cash_atomic, initial_inventory
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9::jsonb
                    )
                    """,
                    participant_id,
                    game_id,
                    user_id,
                    agent_id,
                    hosted["runtime_binding_id"],
                    _json(config_snapshot),
                    config_hash,
                    portfolio.cash_atomic,
                    _json(portfolio.holdings),
                )
                await connection.execute(
                    """
                    INSERT INTO arena402.balances (
                        game_participant_id, cash_atomic, initial_cash_atomic
                    )
                    VALUES ($1, $2, $2)
                    """,
                    participant_id,
                    portfolio.cash_atomic,
                )
                for good_id in GOOD_IDS:
                    quantity = portfolio.holdings[good_id]
                    await connection.execute(
                        """
                        INSERT INTO arena402.holdings (
                            game_participant_id, game_id, good_id, quantity,
                            initial_quantity
                        )
                        VALUES ($1, $2, $3, $4, $4)
                        """,
                        participant_id,
                        game_id,
                        good_id,
                        quantity,
                    )
                if settlement_account is not None:
                    await connection.execute(
                        """
                        INSERT INTO arena402.participant_settlement_accounts (
                            game_participant_id, game_id, chain_id,
                            account_address, custody_mode
                        )
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        participant_id,
                        game_id,
                        settlement_account.chain_id,
                        settlement_account.address,
                        settlement_account.custody_mode,
                    )
                await connection.execute(
                    """
                    UPDATE arena402.games
                    SET phase = 'portfolio_setup'
                    WHERE game_id = $1
                      AND phase = 'registration'
                    """,
                    game_id,
                )
                await self._event(
                    connection,
                    game_id=game_id,
                    event_type="participant.joined",
                    source_key=f"{game_id}:{participant_id}:joined",
                    public_payload={
                        "participantId": participant_id,
                        "agentId": agent_id,
                        "runtimeKind": "hosted",
                    },
                )
        return participant_id

    async def add_connector_participant(
        self,
        *,
        game_id: str,
        user_id: str,
        agent_id: str,
        portfolio: Portfolio,
        settlement_account: SettlementAccount | None = None,
    ) -> str:
        participant_id = f"gp:{game_id}:{agent_id}"
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                phase = await connection.fetchrow(
                    """
                    SELECT phase, max_participants, config_snapshot
                    FROM arena402.games
                    WHERE game_id = $1
                    FOR UPDATE
                    """,
                    game_id,
                )
                if phase is None:
                    raise PawnhouseRepositoryError("game_not_found")
                if phase["phase"] not in (
                    "registration",
                    "portfolio_setup",
                ):
                    raise PawnhouseRepositoryError("game_not_joinable")
                participant_count = int(
                    await connection.fetchval(
                        """
                        SELECT count(*)
                        FROM arena402.game_participants
                        WHERE game_id = $1
                        """,
                        game_id,
                    )
                )
                if participant_count >= int(phase["max_participants"]):
                    raise PawnhouseRepositoryError(
                        "game_participant_limit_reached"
                    )
                game_config = (
                    json.loads(phase["config_snapshot"])
                    if isinstance(phase["config_snapshot"], str)
                    else dict(phase["config_snapshot"])
                )
                settlement_config = self._settlement_config(game_config)
                if settlement_config.authorization_mode != "none":
                    if settlement_account is None:
                        raise PawnhouseRepositoryError(
                            "settlement_account_required"
                        )
                    if (
                        settlement_account.chain_id
                        != settlement_config.chain_id
                    ):
                        raise PawnhouseRepositoryError(
                            "settlement_account_chain_mismatch"
                        )
                connector = await connection.fetchrow(
                    """
                    SELECT
                        a.agent_id,
                        b.runtime_binding_id,
                        b.connector_binding_id,
                        b.connector_binding_epoch
                    FROM public.arena_agents AS a
                    JOIN public.arena_runtime_bindings AS b
                      ON b.agent_id = a.agent_id
                     AND b.runtime_kind = 'connector'
                     AND b.disabled_at IS NULL
                    JOIN LATERAL
                        resolve_connector_binding_for_arena(
                            a.owner_user_id,
                            b.connector_binding_id
                        ) AS route
                      ON route.binding_epoch = b.connector_binding_epoch
                    WHERE a.agent_id = $1
                      AND a.owner_user_id = $2
                      AND a.status = 'active'
                      AND b.route_status = 'ready'
                    """,
                    agent_id,
                    user_id,
                )
                if connector is None:
                    raise PawnhouseRepositoryError(
                        "connector_agent_not_ready"
                    )
                config_snapshot = {
                    "runtime_kind": "connector",
                    "credential_id": None,
                    "connector_binding_id": connector[
                        "connector_binding_id"
                    ],
                    "connector_binding_epoch": int(
                        connector["connector_binding_epoch"]
                    ),
                }
                config_hash = sha256_identifier(config_snapshot)
                await connection.execute(
                    """
                    INSERT INTO arena402.game_participants (
                        game_participant_id, game_id, user_id, agent_id,
                        runtime_binding_id, runtime_kind,
                        portfolio_locked_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, 'connector',
                        clock_timestamp()
                    )
                    """,
                    participant_id,
                    game_id,
                    user_id,
                    agent_id,
                    connector["runtime_binding_id"],
                )
                await connection.execute(
                    """
                    INSERT INTO public.game_agents (
                        game_agent_id, game_id, user_id, agent_id,
                        runtime_binding_id, config_snapshot, config_hash,
                        initial_cash_atomic, initial_inventory
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6::jsonb, $7, $8,
                        $9::jsonb
                    )
                    """,
                    participant_id,
                    game_id,
                    user_id,
                    agent_id,
                    connector["runtime_binding_id"],
                    _json(config_snapshot),
                    config_hash,
                    portfolio.cash_atomic,
                    _json(portfolio.holdings),
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
                    participant_id,
                    portfolio.cash_atomic,
                )
                for good_id in GOOD_IDS:
                    quantity = portfolio.holdings[good_id]
                    await connection.execute(
                        """
                        INSERT INTO arena402.holdings (
                            game_participant_id, game_id, good_id,
                            quantity, initial_quantity
                        )
                        VALUES ($1, $2, $3, $4, $4)
                        """,
                        participant_id,
                        game_id,
                        good_id,
                        quantity,
                    )
                if settlement_account is not None:
                    await connection.execute(
                        """
                        INSERT INTO arena402.participant_settlement_accounts (
                            game_participant_id, game_id, chain_id,
                            account_address, custody_mode
                        )
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        participant_id,
                        game_id,
                        settlement_account.chain_id,
                        settlement_account.address,
                        settlement_account.custody_mode,
                    )
                await connection.execute(
                    """
                    UPDATE arena402.games
                    SET phase = 'portfolio_setup'
                    WHERE game_id = $1
                      AND phase = 'registration'
                    """,
                    game_id,
                )
                await self._event(
                    connection,
                    game_id=game_id,
                    event_type="participant.joined",
                    source_key=f"{game_id}:{participant_id}:joined",
                    public_payload={
                        "participantId": participant_id,
                        "agentId": agent_id,
                        "runtimeKind": "connector",
                    },
                )
        return participant_id

    async def start_game(
        self,
        *,
        game_id: str,
        operator_user_id: str | None = None,
    ) -> dict[str, object]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                game = await connection.fetchrow(
                    """
                    SELECT
                        phase,
                        min_participants,
                        round_count,
                        operator_user_id
                    FROM arena402.games
                    WHERE game_id = $1
                    FOR UPDATE
                    """,
                    game_id,
                )
                if game is None:
                    raise PawnhouseRepositoryError("game_not_found")
                if (
                    operator_user_id is not None
                    and game["operator_user_id"] != operator_user_id
                ):
                    raise PawnhouseRepositoryError(
                        "game_operator_forbidden"
                    )
                if game["phase"] != "portfolio_setup":
                    raise PawnhouseRepositoryError("game_not_ready")
                count = await connection.fetchval(
                    """
                    SELECT count(*)
                    FROM arena402.game_participants
                    WHERE game_id = $1
                      AND portfolio_locked_at IS NOT NULL
                    """,
                    game_id,
                )
                if count < game["min_participants"]:
                    raise PawnhouseRepositoryError("not_enough_participants")
                events = await self._scheduled_events(
                    connection,
                    game_id=game_id,
                )
                if len(events) != int(game["round_count"]):
                    raise PawnhouseRepositoryError(
                        "event_schedule_incomplete"
                    )
                await connection.execute(
                    """
                    UPDATE arena402.games
                    SET phase = 'running',
                        current_round = 1,
                        started_at = clock_timestamp()
                    WHERE game_id = $1
                    """,
                    game_id,
                )
                await connection.execute(
                    """
                    UPDATE arena402.game_participants
                    SET status = 'active'
                    WHERE game_id = $1
                    """,
                    game_id,
                )
                round_id = f"round:{game_id}:1"
                await connection.execute(
                    """
                    INSERT INTO arena402.rounds (
                        round_id, game_id, round_index, phase
                    )
                    VALUES ($1, $2, 1, 'event_reveal')
                    """,
                    round_id,
                    game_id,
                )
                snapshot = WorldState(
                    {event.event_id: event for event in events}
                )
                world_snapshot = snapshot.reveal(
                    events[0].event_id,
                    round_index=1,
                )
                await self._persist_world_snapshot(
                    connection,
                    game_id=game_id,
                    round_id=round_id,
                    event=events[0],
                    snapshot=world_snapshot,
                )
                await connection.execute(
                    """
                    UPDATE arena402.rounds
                    SET phase = 'decide',
                        phase_deadline_at = (
                            clock_timestamp()
                            + (
                                SELECT action_timeout_ms
                                FROM arena402.games
                                WHERE game_id = $2
                            ) * interval '1 millisecond'
                        )
                    WHERE round_id = $1
                    """,
                    round_id,
                    game_id,
                )
                deadline_at = await connection.fetchval(
                    """
                    SELECT phase_deadline_at
                    FROM arena402.rounds
                    WHERE round_id = $1
                    """,
                    round_id,
                )
                await connection.execute(
                    """
                    UPDATE public.games
                    SET status = 'running',
                        started_at = clock_timestamp()
                    WHERE game_id = $1
                    """,
                    game_id,
                )
                await connection.execute(
                    """
                    UPDATE public.game_agents
                    SET status = 'active'
                    WHERE game_id = $1
                    """,
                    game_id,
                )
                await connection.execute(
                    """
                    INSERT INTO public.rounds (
                        round_id, game_id, round_index, phase, deadline_at
                    )
                    VALUES ($1, $2, 1, 'decide', $3)
                    """,
                    round_id,
                    game_id,
                    deadline_at,
                )
        return {"gameId": game_id, "roundId": round_id, "phase": "decide"}

    async def run_rule_market(self, *, game_id: str) -> dict[str, object]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                round_row = await connection.fetchrow(
                    """
                    SELECT r.round_id, r.phase, r.round_index
                    FROM arena402.rounds AS r
                    JOIN arena402.games AS g ON g.game_id = r.game_id
                    WHERE r.game_id = $1
                      AND r.round_index = g.current_round
                    FOR UPDATE OF r
                    """,
                    game_id,
                )
                if round_row is None or round_row["phase"] != "decide":
                    raise PawnhouseRepositoryError("round_not_in_decide")
                configs = await connection.fetch(
                    """
                    SELECT
                        p.game_participant_id,
                        c.intent,
                        c.good_id,
                        c.target_price_atomic,
                        c.public_message
                    FROM arena402.game_participants AS p
                    JOIN arena402.rule_runtime_configs AS c
                      ON c.game_participant_id = p.game_participant_id
                    WHERE p.game_id = $1
                      AND p.status = 'active'
                    ORDER BY p.joined_at, p.game_participant_id
                    """,
                    game_id,
                )
                decisions: list[dict[str, object]] = []
                for row in configs:
                    participant_id = row["game_participant_id"]
                    runtime = RuleRuntime(
                        RuleStrategy(
                            intent=row["intent"],
                            good=row["good_id"],
                            target_price_atomic=int(row["target_price_atomic"]),
                            public_message=row["public_message"],
                        )
                    )
                    decision = runtime.decide()
                    source_result_id = (
                        f"rule-result:{round_row['round_id']}:{participant_id}"
                    )
                    if decision.action != "pass":
                        if decision.action == "sell":
                            quantity = await connection.fetchval(
                                """
                                SELECT quantity
                                FROM arena402.holdings
                                WHERE game_participant_id = $1
                                  AND good_id = $2
                                FOR SHARE
                                """,
                                participant_id,
                                decision.good,
                            )
                            if quantity is None or quantity < 1:
                                raise PawnhouseRepositoryError(
                                    "seller_has_no_inventory"
                                )
                        pool_entry_id = (
                            f"pool:{round_row['round_id']}:{participant_id}"
                        )
                        inserted = await connection.fetchrow(
                            """
                            INSERT INTO arena402.pool_entries (
                                pool_entry_id, game_id, round_id,
                                game_participant_id, source_result_id,
                                side, good_id
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, $7)
                            RETURNING result_received_at
                            """,
                            pool_entry_id,
                            game_id,
                            round_row["round_id"],
                            participant_id,
                            source_result_id,
                            decision.action,
                            decision.good,
                        )
                        received_at = inserted["result_received_at"]
                    else:
                        received_at = await connection.fetchval(
                            "SELECT clock_timestamp()"
                        )
                    public = {
                        "participantId": participant_id,
                        "action": decision.action,
                        "good": decision.good,
                        "resultReceivedAt": received_at.isoformat(),
                    }
                    decisions.append(public)
                    await self._event(
                        connection,
                        game_id=game_id,
                        round_id=round_row["round_id"],
                        event_type="decision.applied",
                        source_key=source_result_id,
                        public_payload=public,
                    )
                await connection.execute(
                    """
                    UPDATE arena402.rounds
                    SET phase = 'match',
                        phase_deadline_at = NULL
                    WHERE round_id = $1
                    """,
                    round_row["round_id"],
                )

                pairings = await self._pair_locked_round(
                    connection,
                    game_id=game_id,
                    round_id=round_row["round_id"],
                )
                await connection.execute(
                    """
                    UPDATE arena402.rounds
                    SET phase = 'negotiate'
                    WHERE round_id = $1
                    """,
                    round_row["round_id"],
                )
                negotiations = await self._run_rule_negotiations(
                    connection,
                    game_id=game_id,
                    round_id=round_row["round_id"],
                )
        return {
            "gameId": game_id,
            "roundId": round_row["round_id"],
            "decisions": decisions,
            "pairings": [self._pairing_public(value) for value in pairings],
            "negotiations": negotiations,
        }

    async def enqueue_agent_runtime_run(
        self,
        *,
        game_id: str,
    ) -> dict[str, object]:
        """Queue one task-driven run for Hosted and/or Connector Agents."""

        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                round_row = await connection.fetchrow(
                    """
                    SELECT r.round_id, r.phase
                    FROM arena402.rounds AS r
                    JOIN arena402.games AS g ON g.game_id = r.game_id
                    WHERE r.game_id = $1
                      AND r.round_index = g.current_round
                    FOR SHARE OF r
                    """,
                    game_id,
                )
                if round_row is None:
                    raise PawnhouseRepositoryError("game_not_found")
                if round_row["phase"] != "decide":
                    raise PawnhouseRepositoryError("round_not_in_decide")
                counts = await connection.fetchrow(
                    """
                    SELECT
                        count(*) AS total,
                        count(*) FILTER (
                            WHERE runtime_kind IN ('hosted', 'connector')
                        ) AS agent_runtime,
                        count(*) FILTER (
                            WHERE runtime_kind = 'connector'
                        ) AS connector
                    FROM arena402.game_participants
                    WHERE game_id = $1
                      AND status = 'active'
                    """,
                    game_id,
                )
                if (
                    counts["total"] < 2
                    or counts["total"] != counts["agent_runtime"]
                ):
                    raise PawnhouseRepositoryError(
                        "agent_runtime_run_requires_hosted_or_connector_participants"
                    )
                runtime_kind = (
                    "mixed" if counts["connector"] else "hosted"
                )
                run_id = f"{runtime_kind}-run:{round_row['round_id']}"
                await connection.execute(
                    """
                    INSERT INTO arena402.runtime_runs (
                        runtime_run_id, game_id, round_id, runtime_kind
                    )
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (round_id, runtime_kind) DO NOTHING
                    """,
                    run_id,
                    game_id,
                    round_row["round_id"],
                    runtime_kind,
                )
                await self._event(
                    connection,
                    game_id=game_id,
                    round_id=round_row["round_id"],
                    event_type="runtime.run_queued",
                    source_key=f"{run_id}:queued",
                    public_payload={
                        "runtimeRunId": run_id,
                        "runtimeKind": runtime_kind,
                    },
                )
        return {
            "gameId": game_id,
            "roundId": round_row["round_id"],
            "runtimeRunId": run_id,
            "status": "queued",
        }

    async def enqueue_hosted_run(self, *, game_id: str) -> dict[str, object]:
        """Compatibility wrapper for the former Hosted-only entrypoint."""

        return await self.enqueue_agent_runtime_run(game_id=game_id)

    async def claim_hosted_run(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> dict[str, object] | None:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            WITH candidate AS (
                SELECT runtime_run_id
                FROM arena402.runtime_runs
                WHERE runtime_kind IN ('hosted', 'mixed')
                  AND (
                      status = 'queued'
                      OR (
                          status IN ('leased', 'running')
                          AND lease_expires_at <= clock_timestamp()
                      )
                  )
                ORDER BY created_at, runtime_run_id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE arena402.runtime_runs AS run
            SET status = 'leased',
                leased_by = $1,
                lease_expires_at = (
                    clock_timestamp()
                    + $2 * interval '1 second'
                ),
                started_at = COALESCE(started_at, clock_timestamp())
            FROM candidate
            WHERE run.runtime_run_id = candidate.runtime_run_id
            RETURNING
                run.runtime_run_id, run.game_id, run.round_id, run.stage
            """,
            worker_id,
            lease_seconds,
        )
        return None if row is None else dict(row)

    async def mark_hosted_run_running(
        self,
        *,
        runtime_run_id: str,
        worker_id: str,
        stage: str,
        lease_seconds: int,
    ) -> None:
        changed = await self._require_pool().fetchval(
            """
            UPDATE arena402.runtime_runs
            SET status = 'running',
                stage = $3,
                lease_expires_at = (
                    clock_timestamp()
                    + $4 * interval '1 second'
                )
            WHERE runtime_run_id = $1
              AND leased_by = $2
              AND status IN ('leased', 'running')
            RETURNING true
            """,
            runtime_run_id,
            worker_id,
            stage,
            lease_seconds,
        )
        if not changed:
            raise PawnhouseRepositoryError("runtime_run_lease_lost")

    async def complete_hosted_run(
        self,
        *,
        runtime_run_id: str,
        worker_id: str,
        error_code: str | None = None,
    ) -> None:
        status = "completed" if error_code is None else "failed"
        changed = await self._require_pool().fetchrow(
            """
            UPDATE arena402.runtime_runs
            SET status = $3,
                stage = (
                    CASE WHEN $3 = 'completed' THEN 'completed' ELSE stage END
                ),
                safe_error_code = $4,
                lease_expires_at = NULL,
                completed_at = clock_timestamp()
            WHERE runtime_run_id = $1
              AND leased_by = $2
              AND status IN ('leased', 'running')
            RETURNING game_id, round_id
            """,
            runtime_run_id,
            worker_id,
            status,
            error_code,
        )
        if changed is None:
            raise PawnhouseRepositoryError("runtime_run_lease_lost")
        async with self._require_pool().acquire() as connection:
            await self._event(
                connection,
                game_id=changed["game_id"],
                round_id=changed["round_id"],
                event_type=f"runtime.run_{status}",
                source_key=f"{runtime_run_id}:{status}",
                public_payload={
                    "runtimeRunId": runtime_run_id,
                    "status": status,
                    "errorCode": error_code,
                },
            )

    async def hosted_decide_contexts(
        self,
        *,
        game_id: str,
    ) -> list[dict[str, object]]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            round_row = await connection.fetchrow(
                """
                SELECT
                    r.round_id, r.round_index, r.phase_deadline_at
                FROM arena402.rounds AS r
                JOIN arena402.games AS g ON g.game_id = r.game_id
                WHERE r.game_id = $1
                  AND r.round_index = g.current_round
                  AND r.phase = 'decide'
                """,
                game_id,
            )
            if round_row is None:
                raise PawnhouseRepositoryError("round_not_in_decide")
            market_rows = await connection.fetch(
                """
                SELECT good_id, market_price_atomic
                FROM arena402.price_snapshots
                WHERE game_id = $1
                  AND round_index = (
                      SELECT current_round
                      FROM arena402.games
                      WHERE game_id = $1
                  )
                ORDER BY good_id
                """,
                game_id,
            )
            event_rows = await connection.fetch(
                """
                SELECT
                    event_id, public_snapshot, revealed_at
                FROM arena402.event_occurrences
                WHERE game_id = $1
                  AND round_index <= (
                      SELECT current_round
                      FROM arena402.games
                      WHERE game_id = $1
                  )
                ORDER BY round_index, event_id
                """,
                game_id,
            )
            participants = await connection.fetch(
                """
                SELECT
                    p.game_participant_id,
                    b.cash_atomic,
                    ga.config_snapshot,
                    ga.config_hash
                FROM arena402.game_participants AS p
                JOIN arena402.balances AS b
                  ON b.game_participant_id = p.game_participant_id
                JOIN public.game_agents AS ga
                  ON ga.game_agent_id = p.game_participant_id
                WHERE p.game_id = $1
                  AND p.runtime_kind IN ('hosted', 'connector')
                  AND p.status = 'active'
                ORDER BY p.joined_at, p.game_participant_id
                """,
                game_id,
            )
            contexts: list[dict[str, object]] = []
            for participant in participants:
                holding_rows = await connection.fetch(
                    """
                    SELECT good_id, quantity
                    FROM arena402.holdings
                    WHERE game_participant_id = $1
                    ORDER BY good_id
                    """,
                    participant["game_participant_id"],
                )
                contexts.append(
                    {
                        "game_id": game_id,
                        "round_id": round_row["round_id"],
                        "round_index": round_row["round_index"],
                        "deadline_at": round_row["phase_deadline_at"],
                        "participant_id": participant[
                            "game_participant_id"
                        ],
                        "cash_atomic": int(participant["cash_atomic"]),
                        "holdings": {
                            row["good_id"]: int(row["quantity"])
                            for row in holding_rows
                        },
                        "market": {
                            row["good_id"]: int(
                                row["market_price_atomic"]
                            )
                            for row in market_rows
                        },
                        "events": [
                            {
                                "event_id": row["event_id"],
                                "payload": (
                                    json.loads(row["public_snapshot"])
                                    if isinstance(
                                        row["public_snapshot"],
                                        str,
                                    )
                                    else dict(row["public_snapshot"])
                                ),
                                "occurred_at": row["revealed_at"],
                            }
                            for row in event_rows
                        ],
                        "config_snapshot": (
                            json.loads(participant["config_snapshot"])
                            if isinstance(
                                participant["config_snapshot"],
                                str,
                            )
                            else dict(participant["config_snapshot"])
                        ),
                        "config_hash": participant["config_hash"],
                    }
                )
        return contexts

    async def apply_hosted_decision(
        self,
        *,
        game_id: str,
        round_id: str,
        participant_id: str,
        result_id: str,
        result_received_at: datetime,
        action: Mapping[str, object],
    ) -> dict[str, object]:
        action_name = str(action.get("action", "pass"))
        good_id = action.get("good")
        if action_name not in {"buy", "sell", "pass"}:
            action_name = "pass"
            good_id = None
        if action_name in {"buy", "sell"} and good_id not in GOOD_IDS:
            action_name = "pass"
            good_id = None
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                exists = await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM arena402.game_events
                        WHERE game_id = $1
                          AND source_idempotency_key = $2
                    )
                    """,
                    game_id,
                    result_id,
                )
                if exists:
                    return {
                        "participantId": participant_id,
                        "action": action_name,
                        "good": good_id,
                        "resultReceivedAt": result_received_at.isoformat(),
                    }
                if action_name == "sell":
                    available = await connection.fetchval(
                        """
                        SELECT quantity
                        FROM arena402.holdings
                        WHERE game_participant_id = $1
                          AND good_id = $2
                        FOR SHARE
                        """,
                        participant_id,
                        good_id,
                    )
                    if available is None or available < 1:
                        action_name = "pass"
                        good_id = None
                if action_name != "pass":
                    await connection.execute(
                        """
                        INSERT INTO arena402.pool_entries (
                            pool_entry_id, game_id, round_id,
                            game_participant_id, source_result_id,
                            side, good_id, result_received_at
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT (source_result_id) DO NOTHING
                        """,
                        f"pool:{round_id}:{participant_id}",
                        game_id,
                        round_id,
                        participant_id,
                        result_id,
                        action_name,
                        good_id,
                        result_received_at,
                    )
                public = {
                    "participantId": participant_id,
                    "action": action_name,
                    "good": good_id,
                    "resultReceivedAt": result_received_at.isoformat(),
                }
                await self._event(
                    connection,
                    game_id=game_id,
                    round_id=round_id,
                    event_type="decision.applied",
                    source_key=result_id,
                    public_payload=public,
                )
        return public

    async def pair_hosted_round(
        self,
        *,
        game_id: str,
        round_id: str,
    ) -> tuple[Pairing, ...]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE arena402.rounds
                    SET phase = 'match',
                        phase_deadline_at = NULL
                    WHERE round_id = $1
                      AND phase = 'decide'
                    """,
                    round_id,
                )
                pairings = await self._pair_locked_round(
                    connection,
                    game_id=game_id,
                    round_id=round_id,
                )
                await connection.execute(
                    """
                    UPDATE arena402.rounds
                    SET phase = 'negotiate'
                    WHERE round_id = $1
                      AND phase = 'match'
                    """,
                    round_id,
                )
                await connection.execute(
                    """
                    UPDATE public.rounds
                    SET phase = 'negotiate'
                    WHERE round_id = $1
                    """,
                    round_id,
                )
        return pairings

    async def hosted_negotiation_context(
        self,
        *,
        negotiation_id: str,
    ) -> dict[str, object] | None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            negotiation = await connection.fetchrow(
                """
                SELECT *
                FROM arena402.negotiations
                WHERE negotiation_id = $1
                """,
                negotiation_id,
            )
            if negotiation is None or negotiation["status"] != "active":
                return None
            role = negotiation["next_role"]
            participant_id = (
                negotiation["buyer_participant_id"]
                if role == "buyer"
                else negotiation["seller_participant_id"]
            )
            counterparty_id = (
                negotiation["seller_participant_id"]
                if role == "buyer"
                else negotiation["buyer_participant_id"]
            )
            participant = await connection.fetchrow(
                """
                SELECT
                    p.agent_id,
                    b.cash_atomic,
                    h.quantity AS inventory_available,
                    ga.config_snapshot,
                    ga.config_hash
                FROM arena402.game_participants AS p
                JOIN arena402.balances AS b
                  ON b.game_participant_id = p.game_participant_id
                JOIN arena402.holdings AS h
                  ON h.game_participant_id = p.game_participant_id
                 AND h.good_id = (
                     SELECT good_id
                     FROM arena402.pairings
                     WHERE pairing_id = $2
                 )
                JOIN public.game_agents AS ga
                  ON ga.game_agent_id = p.game_participant_id
                WHERE p.game_participant_id = $1
                """,
                participant_id,
                negotiation["pairing_id"],
            )
            counterparty = await connection.fetchrow(
                """
                SELECT p.agent_id, a.name
                FROM arena402.game_participants AS p
                LEFT JOIN public.arena_agents AS a
                  ON a.agent_id = p.agent_id
                WHERE p.game_participant_id = $1
                """,
                counterparty_id,
            )
            messages = await connection.fetch(
                """
                SELECT
                    turn_sequence, actor_role, action,
                    price_atomic, public_message, created_at
                FROM arena402.negotiation_messages
                WHERE negotiation_id = $1
                ORDER BY turn_sequence
                """,
                negotiation_id,
            )
            good_id = await connection.fetchval(
                """
                SELECT good_id
                FROM arena402.pairings
                WHERE pairing_id = $1
                """,
                negotiation["pairing_id"],
            )
            round_index = await connection.fetchval(
                """
                SELECT round_index
                FROM arena402.rounds
                WHERE round_id = $1
                """,
                negotiation["round_id"],
            )
            event_rows = await connection.fetch(
                """
                SELECT event_id, public_snapshot, revealed_at
                FROM arena402.event_occurrences
                WHERE game_id = $1
                  AND round_index <= $2
                ORDER BY round_index, event_id
                """,
                negotiation["game_id"],
                round_index,
            )
        latest_quote = next(
            (
                message
                for message in reversed(messages)
                if message["action"] == "propose"
                and message["actor_role"] != role
            ),
            None,
        )
        return {
            "game_id": negotiation["game_id"],
            "round_id": negotiation["round_id"],
            "round_index": round_index,
            "negotiation_id": negotiation_id,
            "participant_id": participant_id,
            "counterparty_id": counterparty_id,
            "counterparty_agent_id": counterparty["agent_id"],
            "counterparty_name": counterparty["name"] or "Arena Agent",
            "role": role,
            "good": good_id,
            "cash_atomic": int(participant["cash_atomic"]),
            "inventory_available": int(
                participant["inventory_available"]
            ),
            "turn_sequence": int(negotiation["turn_count"]) + 1,
            "remaining_turns": int(negotiation["max_turns"])
            - int(negotiation["turn_count"]),
            "deadline_at": negotiation["action_deadline_at"],
            "history": [
                {
                    "turn_sequence": int(message["turn_sequence"]),
                    "from_role": message["actor_role"],
                    "action": message["action"],
                    "price_atomic": (
                        int(message["price_atomic"])
                        if message["price_atomic"] is not None
                        else None
                    ),
                    "message": message["public_message"],
                }
                for message in messages
            ],
            "latest_quote": (
                None
                if latest_quote is None
                else {
                    "turn_sequence": int(latest_quote["turn_sequence"]),
                    "from_role": latest_quote["actor_role"],
                    "price_atomic": int(latest_quote["price_atomic"]),
                }
            ),
            "events": [
                {
                    "event_id": row["event_id"],
                    "payload": (
                        json.loads(row["public_snapshot"])
                        if isinstance(row["public_snapshot"], str)
                        else dict(row["public_snapshot"])
                    ),
                    "occurred_at": row["revealed_at"],
                }
                for row in event_rows
            ],
            "config_snapshot": (
                json.loads(participant["config_snapshot"])
                if isinstance(participant["config_snapshot"], str)
                else dict(participant["config_snapshot"])
            ),
            "config_hash": participant["config_hash"],
        }

    async def active_hosted_negotiation_ids(
        self,
        *,
        game_id: str,
        round_id: str,
    ) -> list[str]:
        rows = await self._require_pool().fetch(
            """
            SELECT negotiation_id
            FROM arena402.negotiations
            WHERE game_id = $1
              AND round_id = $2
              AND status = 'active'
            ORDER BY created_at, negotiation_id
            """,
            game_id,
            round_id,
        )
        return [row["negotiation_id"] for row in rows]

    async def apply_hosted_negotiation_action(
        self,
        *,
        negotiation_id: str,
        result_id: str,
        action: Mapping[str, object] | None,
    ) -> dict[str, object]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT *
                    FROM arena402.negotiations
                    WHERE negotiation_id = $1
                    FOR UPDATE
                    """,
                    negotiation_id,
                )
                if row is None:
                    raise PawnhouseRepositoryError(
                        "negotiation_not_found"
                    )
                applied = await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM arena402.negotiation_messages
                        WHERE source_result_id = $1
                    )
                    """,
                    result_id,
                )
                if applied:
                    return {
                        "negotiationId": negotiation_id,
                        "status": row["status"],
                        "turnCount": int(row["turn_count"]),
                    }
                if row["status"] != "active":
                    return {
                        "negotiationId": negotiation_id,
                        "status": row["status"],
                        "turnCount": int(row["turn_count"]),
                    }
                negotiation = Negotiation(
                    negotiation_id=negotiation_id,
                    buyer_participant_id=row["buyer_participant_id"],
                    seller_participant_id=row["seller_participant_id"],
                    max_turns=int(row["max_turns"]),
                )
                previous = await connection.fetch(
                    """
                    SELECT
                        turn_sequence, actor_role, action,
                        price_atomic, public_message
                    FROM arena402.negotiation_messages
                    WHERE negotiation_id = $1
                    ORDER BY turn_sequence
                    """,
                    negotiation_id,
                )
                for message in previous:
                    negotiation.apply(
                        role=message["actor_role"],
                        action=NegotiationAction(
                            action=message["action"],
                            price_atomic=(
                                int(message["price_atomic"])
                                if message["price_atomic"] is not None
                                else None
                            ),
                            message=message["public_message"],
                        ),
                    )
                if action is None:
                    negotiation.expire()
                    action_value = NegotiationAction(
                        action="reject",
                        message="The Arena action deadline expired.",
                    )
                else:
                    action_name = str(action.get("action"))
                    action_value = NegotiationAction(
                        action=action_name,
                        price_atomic=(
                            gold(str(action["price"]))
                            if action_name == "propose"
                            and action.get("price") is not None
                            else None
                        ),
                        message=(
                            str(action["message"])
                            if action.get("message") is not None
                            else None
                        ),
                    )
                    turn = negotiation.apply(
                        role=row["next_role"],
                        action=action_value,
                    )
                    await connection.execute(
                        """
                        INSERT INTO arena402.negotiation_messages (
                            negotiation_message_id, negotiation_id, game_id,
                            round_id, source_result_id, turn_sequence,
                            actor_role, action, price_atomic, public_message
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10
                        )
                        """,
                        f"msg:{negotiation_id}:{turn.sequence}",
                        negotiation_id,
                        row["game_id"],
                        row["round_id"],
                        result_id,
                        turn.sequence,
                        turn.role,
                        turn.action.action,
                        turn.action.price_atomic,
                        turn.action.message,
                    )
                    await self._event(
                        connection,
                        game_id=row["game_id"],
                        round_id=row["round_id"],
                        event_type="negotiation.message",
                        source_key=result_id,
                        public_payload={
                            "negotiationId": negotiation_id,
                            "turn": turn.sequence,
                            "role": turn.role,
                            "action": turn.action.action,
                            "priceAtomic": (
                                str(turn.action.price_atomic)
                                if turn.action.price_atomic is not None
                                else None
                            ),
                            "message": turn.action.message,
                        },
                    )
                latest_proposal = next(
                    (
                        turn
                        for turn in reversed(negotiation.turns)
                        if turn.action.action == "propose"
                    ),
                    None,
                )
                completed_at = (
                    datetime.now(timezone.utc)
                    if negotiation.status is not NegotiationStatus.ACTIVE
                    else None
                )
                await connection.execute(
                    """
                    UPDATE arena402.negotiations
                    SET turn_count = $2,
                        next_role = $3,
                        status = $4,
                        latest_proposal_price_atomic = $5,
                        latest_proposal_role = $6,
                        accepted_price_atomic = $7,
                        completed_at = $8
                    WHERE negotiation_id = $1
                    """,
                    negotiation_id,
                    len(negotiation.turns),
                    (
                        negotiation.next_role
                        if negotiation.status is NegotiationStatus.ACTIVE
                        else "none"
                    ),
                    negotiation.status.value,
                    (
                        latest_proposal.action.price_atomic
                        if latest_proposal is not None
                        else None
                    ),
                    (
                        latest_proposal.role
                        if latest_proposal is not None
                        else None
                    ),
                    negotiation.accepted_price_atomic,
                    completed_at,
                )
                if negotiation.status is not NegotiationStatus.ACTIVE:
                    await connection.execute(
                        """
                        UPDATE arena402.pairings
                        SET status = $2::text,
                            completed_at = (
                                CASE
                                    WHEN $2::text IN ('rejected', 'timeout')
                                    THEN $3::timestamptz
                                    ELSE NULL::timestamptz
                                END
                            )
                        WHERE pairing_id = $1
                        """,
                        row["pairing_id"],
                        negotiation.status.value,
                        completed_at,
                    )
                    if (
                        negotiation.status
                        is NegotiationStatus.ACCEPTED_PENDING_SETTLEMENT
                    ):
                        await self._freeze_settlement_intent(
                            connection,
                            negotiation_id=negotiation_id,
                        )
        return {
            "negotiationId": negotiation_id,
            "status": negotiation.status.value,
            "turnCount": len(negotiation.turns),
            "acceptedPriceAtomic": (
                str(negotiation.accepted_price_atomic)
                if negotiation.accepted_price_atomic is not None
                else None
            ),
        }

    async def hosted_run_status(
        self,
        *,
        game_id: str,
    ) -> dict[str, object] | None:
        row = await self._require_pool().fetchrow(
            """
            SELECT
                runtime_run_id, round_id, status, stage,
                safe_error_code, created_at, started_at, completed_at
            FROM arena402.runtime_runs
            WHERE game_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            game_id,
        )
        return None if row is None else dict(row)

    async def settlement_intents_for_game(
        self,
        *,
        game_id: str,
    ) -> list[dict[str, object]]:
        rows = await self._require_pool().fetch(
            """
            SELECT
                i.*,
                s.tx_hash,
                s.submission_source,
                a.approval_source,
                c.block_number,
                c.block_hash,
                c.confirmation_count
            FROM arena402.settlement_intents AS i
            LEFT JOIN arena402.settlement_submissions AS s
              ON s.settlement_intent_id = i.settlement_intent_id
            LEFT JOIN arena402.settlement_approvals AS a
              ON a.settlement_intent_id = i.settlement_intent_id
            LEFT JOIN arena402.settlement_confirmations AS c
              ON c.settlement_intent_id = i.settlement_intent_id
            WHERE i.game_id = $1
            ORDER BY i.created_at, i.settlement_intent_id
            """,
            game_id,
        )
        return [self._settlement_public(row) for row in rows]

    async def recoverable_settlement_targets(
        self,
        *,
        limit: int = 50,
    ) -> list[dict[str, str]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        rows = await self._require_pool().fetch(
            """
            SELECT settlement_intent_id, status
            FROM arena402.settlement_intents
            WHERE status IN (
                'submitted',
                'confirmation_timeout',
                'chain_confirmed_uncommitted'
            )
            ORDER BY created_at, settlement_intent_id
            LIMIT $1
            """,
            limit,
        )
        return [
            {
                "settlement_intent_id": str(
                    row["settlement_intent_id"]
                ),
                "status": str(row["status"]),
            }
            for row in rows
        ]

    async def record_settlement_approval(
        self,
        *,
        settlement_intent_id: str,
        approved_intent_hash: str,
        authorization_nonce: str,
        approval_source: str,
    ) -> dict[str, object]:
        if not _INTENT_HASH.fullmatch(approved_intent_hash):
            raise PawnhouseRepositoryError("invalid_approved_intent_hash")
        if approval_source not in {"operator_cli", "payment_mandate"}:
            raise PawnhouseRepositoryError("invalid_approval_source")
        normalized_nonce = normalize_authorization_nonce(
            authorization_nonce
        )
        nonce_digest = sha256_text_identifier(normalized_nonce)
        expected_nonce = (
            "0x" + approved_intent_hash.removeprefix("sha256:")
        )
        if normalized_nonce != expected_nonce:
            raise PawnhouseRepositoryError(
                "authorization_nonce_not_bound_to_intent"
            )
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                intent = await connection.fetchrow(
                    """
                    SELECT *
                    FROM arena402.settlement_intents
                    WHERE settlement_intent_id = $1
                    FOR UPDATE
                    """,
                    settlement_intent_id,
                )
                if intent is None:
                    raise PawnhouseRepositoryError(
                        "settlement_intent_not_found"
                    )
                if intent["intent_hash"] != approved_intent_hash:
                    raise PawnhouseRepositoryError(
                        "approved_intent_hash_mismatch"
                    )
                existing = await connection.fetchrow(
                    """
                    SELECT
                        approved_intent_hash,
                        authorization_nonce_digest,
                        approval_source
                    FROM arena402.settlement_approvals
                    WHERE settlement_intent_id = $1
                    """,
                    settlement_intent_id,
                )
                if existing is not None:
                    if (
                        existing["approved_intent_hash"]
                        != approved_intent_hash
                        or existing["authorization_nonce_digest"]
                        != nonce_digest
                        or existing["approval_source"] != approval_source
                    ):
                        raise PawnhouseRepositoryError(
                            "settlement_approval_conflict"
                        )
                else:
                    if intent["status"] != "authorization_requested":
                        raise PawnhouseRepositoryError(
                            "settlement_not_awaiting_approval"
                        )
                    await connection.execute(
                        """
                        INSERT INTO arena402.settlement_approvals (
                            settlement_intent_id,
                            approved_intent_hash,
                            authorization_nonce_digest,
                            approval_source
                        )
                        VALUES ($1, $2, $3, $4)
                        """,
                        settlement_intent_id,
                        approved_intent_hash,
                        nonce_digest,
                        approval_source,
                    )
                    await self._event(
                        connection,
                        game_id=intent["game_id"],
                        round_id=intent["round_id"],
                        event_type="settlement.approved",
                        source_key=f"{settlement_intent_id}:approved",
                        public_payload={
                            "settlementIntentId": settlement_intent_id,
                            "intentHash": approved_intent_hash,
                            "approvalSource": approval_source,
                            "status": "authorization_requested",
                        },
                    )
                value = dict(intent)
                value.update(
                    {
                        "approval_source": approval_source,
                        "tx_hash": None,
                        "submission_source": None,
                        "block_number": None,
                        "block_hash": None,
                        "confirmation_count": None,
                    }
                )
                return self._settlement_public(value)

    async def record_settlement_submission(
        self,
        *,
        settlement_intent_id: str,
        tx_hash: str,
        authorization_nonce: str,
        approved_intent_hash: str,
        submission_source: str,
    ) -> dict[str, object]:
        normalized_tx = normalize_tx_hash(tx_hash)
        normalized_nonce = normalize_authorization_nonce(
            authorization_nonce
        )
        if not _INTENT_HASH.fullmatch(approved_intent_hash):
            raise PawnhouseRepositoryError("invalid_approved_intent_hash")
        if submission_source not in {"wallet", "sandbox_guest"}:
            raise PawnhouseRepositoryError("invalid_submission_source")
        nonce_digest = sha256_text_identifier(normalized_nonce)
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                intent = await connection.fetchrow(
                    """
                    SELECT *
                    FROM arena402.settlement_intents
                    WHERE settlement_intent_id = $1
                    FOR UPDATE
                    """,
                    settlement_intent_id,
                )
                if intent is None:
                    raise PawnhouseRepositoryError(
                        "settlement_intent_not_found"
                    )
                approval = await connection.fetchrow(
                    """
                    SELECT
                        approved_intent_hash,
                        authorization_nonce_digest,
                        approval_source
                    FROM arena402.settlement_approvals
                    WHERE settlement_intent_id = $1
                    """,
                    settlement_intent_id,
                )
                existing = await connection.fetchrow(
                    """
                    SELECT
                        tx_hash, authorization_nonce_digest,
                        submission_source
                    FROM arena402.settlement_submissions
                    WHERE settlement_intent_id = $1
                    """,
                    settlement_intent_id,
                )
                if existing is not None:
                    if (
                        existing["tx_hash"] != normalized_tx
                        or existing["authorization_nonce_digest"]
                        != nonce_digest
                        or existing["submission_source"] != submission_source
                    ):
                        raise PawnhouseRepositoryError(
                            "settlement_submission_conflict"
                        )
                    return self._settlement_public(
                        {
                            **dict(intent),
                            "tx_hash": existing["tx_hash"],
                            "submission_source": existing[
                                "submission_source"
                            ],
                            "approval_source": (
                                approval["approval_source"]
                                if approval is not None
                                else None
                            ),
                            "block_number": None,
                            "block_hash": None,
                            "confirmation_count": None,
                        }
                    )
                if approval is None:
                    raise PawnhouseRepositoryError(
                        "settlement_approval_required"
                    )
                if (
                    approval["approved_intent_hash"]
                    != approved_intent_hash
                    or approval["approved_intent_hash"]
                    != intent["intent_hash"]
                    or approval["authorization_nonce_digest"]
                    != nonce_digest
                    or approval["approval_source"]
                    not in {"operator_cli", "payment_mandate"}
                ):
                    raise PawnhouseRepositoryError(
                        "settlement_approval_mismatch"
                    )
                if intent["status"] not in (
                    "authorization_requested",
                    "confirmation_timeout",
                ):
                    raise PawnhouseRepositoryError(
                        "settlement_not_awaiting_submission"
                    )
                await connection.execute(
                    """
                    INSERT INTO arena402.settlement_submissions (
                        settlement_intent_id, tx_hash,
                        authorization_nonce_digest, submission_source
                    )
                    VALUES ($1, $2, $3, $4)
                    """,
                    settlement_intent_id,
                    normalized_tx,
                    nonce_digest,
                    submission_source,
                )
                await connection.execute(
                    """
                    UPDATE arena402.settlement_intents
                    SET status = 'submitted',
                        submitted_at = clock_timestamp(),
                        safe_error_code = NULL
                    WHERE settlement_intent_id = $1
                    """,
                    settlement_intent_id,
                )
                await self._event(
                    connection,
                    game_id=intent["game_id"],
                    round_id=intent["round_id"],
                    event_type="settlement.submitted",
                    source_key=f"{settlement_intent_id}:submitted",
                    public_payload={
                        "settlementIntentId": settlement_intent_id,
                        "txHash": normalized_tx,
                        "status": "submitted",
                    },
                )
                updated = dict(intent)
                updated.update(
                    {
                        "status": "submitted",
                        "tx_hash": normalized_tx,
                        "submission_source": submission_source,
                        "approval_source": approval["approval_source"],
                        "block_number": None,
                        "block_hash": None,
                        "confirmation_count": None,
                    }
                )
                return self._settlement_public(updated)

    async def mark_confirmation_timeout(
        self,
        *,
        settlement_intent_id: str,
    ) -> dict[str, object]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE arena402.settlement_intents
                    SET status = 'confirmation_timeout',
                        safe_error_code = 'confirmation_timeout'
                    WHERE settlement_intent_id = $1
                      AND status = 'submitted'
                    RETURNING *
                    """,
                    settlement_intent_id,
                )
                if row is None:
                    row = await connection.fetchrow(
                        """
                        SELECT *
                        FROM arena402.settlement_intents
                        WHERE settlement_intent_id = $1
                        """,
                        settlement_intent_id,
                    )
                    if row is None:
                        raise PawnhouseRepositoryError(
                            "settlement_intent_not_found"
                        )
                    if row["status"] != "confirmation_timeout":
                        raise PawnhouseRepositoryError(
                            "settlement_not_submitted"
                        )
                submission = await connection.fetchrow(
                    """
                    SELECT tx_hash, submission_source
                    FROM arena402.settlement_submissions
                    WHERE settlement_intent_id = $1
                    """,
                    settlement_intent_id,
                )
                await self._event(
                    connection,
                    game_id=row["game_id"],
                    round_id=row["round_id"],
                    event_type="settlement.confirmation_timeout",
                    source_key=f"{settlement_intent_id}:confirmation_timeout",
                    public_payload={
                        "settlementIntentId": settlement_intent_id,
                        "txHash": (
                            submission["tx_hash"]
                            if submission is not None
                            else None
                        ),
                        "status": "confirmation_timeout",
                    },
                )
                value = dict(row)
                value.update(
                    {
                        "tx_hash": (
                            submission["tx_hash"]
                            if submission is not None
                            else None
                        ),
                        "submission_source": (
                            submission["submission_source"]
                            if submission is not None
                            else None
                        ),
                        "block_number": None,
                        "block_hash": None,
                        "confirmation_count": None,
                    }
                )
                return self._settlement_public(value)

    async def settlement_intent_for_payment(
        self,
        *,
        settlement_intent_id: str,
    ) -> SettlementIntent:
        row = await self._require_pool().fetchrow(
            """
            SELECT *
            FROM arena402.settlement_intents
            WHERE settlement_intent_id = $1
            """,
            settlement_intent_id,
        )
        if row is None:
            raise PawnhouseRepositoryError("settlement_intent_not_found")
        return self._intent_from_row(row)

    async def record_mandate_approval(
        self,
        *,
        settlement_intent_id: str,
        approved_intent_hash: str,
        authorization_nonce: str,
    ) -> None:
        await self.record_settlement_approval(
            settlement_intent_id=settlement_intent_id,
            approved_intent_hash=approved_intent_hash,
            authorization_nonce=authorization_nonce,
            approval_source="payment_mandate",
        )

    async def record_automatic_submission(
        self,
        *,
        settlement_intent_id: str,
        tx_hash: str,
        authorization_nonce: str,
        approved_intent_hash: str,
    ) -> None:
        await self.record_settlement_submission(
            settlement_intent_id=settlement_intent_id,
            tx_hash=tx_hash,
            authorization_nonce=authorization_nonce,
            approved_intent_hash=approved_intent_hash,
            submission_source="sandbox_guest",
        )

    async def record_automatic_failure(
        self,
        *,
        settlement_intent_id: str,
        safe_error_code: str,
    ) -> None:
        if not safe_error_code or len(safe_error_code) > 100:
            raise PawnhouseRepositoryError("invalid_safe_error_code")
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                intent = await connection.fetchrow(
                    """
                    SELECT *
                    FROM arena402.settlement_intents
                    WHERE settlement_intent_id = $1
                    FOR UPDATE
                    """,
                    settlement_intent_id,
                )
                if intent is None:
                    raise PawnhouseRepositoryError(
                        "settlement_intent_not_found"
                    )
                if intent["status"] == "authorization_failed":
                    return
                if intent["status"] != "authorization_requested":
                    raise PawnhouseRepositoryError(
                        "settlement_not_awaiting_authorization"
                    )
                await connection.execute(
                    """
                    UPDATE arena402.settlement_intents
                    SET status = 'authorization_failed',
                        safe_error_code = $2,
                        completed_at = clock_timestamp()
                    WHERE settlement_intent_id = $1
                    """,
                    settlement_intent_id,
                    safe_error_code,
                )
                await connection.execute(
                    """
                    UPDATE arena402.pairings
                    SET status = 'settlement_failed',
                        completed_at = clock_timestamp()
                    WHERE pairing_id = $1
                    """,
                    intent["pairing_id"],
                )
                await connection.execute(
                    """
                    UPDATE arena402.game_participants
                    SET status = 'active'
                    WHERE game_participant_id = ANY($1::text[])
                      AND status = 'settling'
                    """,
                    [
                        intent["buyer_participant_id"],
                        intent["seller_participant_id"],
                    ],
                )
                await self._event(
                    connection,
                    game_id=intent["game_id"],
                    round_id=intent["round_id"],
                    event_type="settlement.authorization_failed",
                    source_key=f"{settlement_intent_id}:authorization_failed",
                    public_payload={
                        "settlementIntentId": settlement_intent_id,
                        "status": "authorization_failed",
                        "safeErrorCode": safe_error_code,
                    },
                )

    async def record_chain_confirmation(
        self,
        *,
        settlement_intent_id: str,
        confirmation: ChainConfirmation,
    ) -> dict[str, object]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT i.*, s.tx_hash, s.submission_source
                    FROM arena402.settlement_intents AS i
                    JOIN arena402.settlement_submissions AS s
                      ON s.settlement_intent_id = i.settlement_intent_id
                    WHERE i.settlement_intent_id = $1
                    FOR UPDATE OF i
                    """,
                    settlement_intent_id,
                )
                if row is None:
                    raise PawnhouseRepositoryError(
                        "settlement_submission_not_found"
                    )
                intent = self._intent_from_row(row)
                if confirmation.tx_hash != row["tx_hash"]:
                    raise PawnhouseRepositoryError(
                        "confirmation_transaction_mismatch"
                    )
                try:
                    validate_chain_confirmation(intent, confirmation)
                except SettlementError as exc:
                    raise PawnhouseRepositoryError(str(exc)) from None
                existing = await connection.fetchrow(
                    """
                    SELECT *
                    FROM arena402.settlement_confirmations
                    WHERE settlement_intent_id = $1
                    """,
                    settlement_intent_id,
                )
                if existing is not None:
                    if existing["evidence_hash"] != confirmation.evidence_hash:
                        raise PawnhouseRepositoryError(
                            "chain_confirmation_conflict"
                        )
                else:
                    if row["status"] not in (
                        "submitted",
                        "confirmation_timeout",
                    ):
                        raise PawnhouseRepositoryError(
                            "settlement_not_confirmable"
                        )
                    await connection.execute(
                        """
                        INSERT INTO arena402.settlement_confirmations (
                            settlement_intent_id, tx_hash, chain_id,
                            token_address, from_account, to_account,
                            amount_atomic, block_number, block_hash,
                            confirmation_count, evidence_hash
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
                        )
                        """,
                        settlement_intent_id,
                        confirmation.tx_hash,
                        confirmation.chain_id,
                        confirmation.token_address,
                        confirmation.from_account,
                        confirmation.to_account,
                        confirmation.amount_atomic,
                        confirmation.block_number,
                        confirmation.block_hash,
                        confirmation.confirmation_count,
                        confirmation.evidence_hash,
                    )
                    await connection.execute(
                        """
                        UPDATE arena402.settlement_intents
                        SET status = 'chain_confirmed_uncommitted',
                            chain_confirmed_at = clock_timestamp(),
                            safe_error_code = NULL
                        WHERE settlement_intent_id = $1
                        """,
                        settlement_intent_id,
                    )
                    await self._event(
                        connection,
                        game_id=row["game_id"],
                        round_id=row["round_id"],
                        event_type="settlement.chain_confirmed",
                        source_key=(
                            f"{settlement_intent_id}:"
                            f"{confirmation.evidence_hash}"
                        ),
                        public_payload={
                            "settlementIntentId": settlement_intent_id,
                            "txHash": confirmation.tx_hash,
                            "blockNumber": confirmation.block_number,
                            "confirmationCount": (
                                confirmation.confirmation_count
                            ),
                            "status": "chain_confirmed_uncommitted",
                        },
                    )
                value = dict(row)
                value.update(
                    {
                        "status": (
                            "inventory_committed"
                            if row["status"] == "inventory_committed"
                            else "chain_confirmed_uncommitted"
                        ),
                        "block_number": confirmation.block_number,
                        "block_hash": confirmation.block_hash,
                        "confirmation_count": (
                            confirmation.confirmation_count
                        ),
                    }
                )
                return self._settlement_public(value)

    async def settlement_confirmation_target(
        self,
        *,
        settlement_intent_id: str,
    ) -> tuple[SettlementIntent, str]:
        row = await self._require_pool().fetchrow(
            """
            SELECT i.*, s.tx_hash
            FROM arena402.settlement_intents AS i
            JOIN arena402.settlement_submissions AS s
              ON s.settlement_intent_id = i.settlement_intent_id
            WHERE i.settlement_intent_id = $1
            """,
            settlement_intent_id,
        )
        if row is None:
            raise PawnhouseRepositoryError(
                "settlement_submission_not_found"
            )
        if row["status"] not in (
            "submitted",
            "confirmation_timeout",
            "chain_confirmed_uncommitted",
            "inventory_committed",
        ):
            raise PawnhouseRepositoryError("settlement_not_recoverable")
        return self._intent_from_row(row), row["tx_hash"]

    async def record_chain_reverted(
        self,
        *,
        settlement_intent_id: str,
        tx_hash: str,
    ) -> dict[str, object]:
        normalized_tx = normalize_tx_hash(tx_hash)
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT i.*, s.tx_hash, s.submission_source
                    FROM arena402.settlement_intents AS i
                    JOIN arena402.settlement_submissions AS s
                      ON s.settlement_intent_id = i.settlement_intent_id
                    WHERE i.settlement_intent_id = $1
                    FOR UPDATE OF i
                    """,
                    settlement_intent_id,
                )
                if row is None:
                    raise PawnhouseRepositoryError(
                        "settlement_submission_not_found"
                    )
                if row["tx_hash"] != normalized_tx:
                    raise PawnhouseRepositoryError(
                        "confirmation_transaction_mismatch"
                    )
                if row["status"] == "reverted":
                    value = dict(row)
                    value.update(
                        {
                            "block_number": None,
                            "block_hash": None,
                            "confirmation_count": None,
                        }
                    )
                    return self._settlement_public(value)
                if row["status"] not in (
                    "submitted",
                    "confirmation_timeout",
                ):
                    raise PawnhouseRepositoryError(
                        "settlement_not_revertible"
                    )
                await connection.execute(
                    """
                    UPDATE arena402.settlement_intents
                    SET status = 'reverted',
                        safe_error_code = 'chain_transaction_reverted',
                        completed_at = clock_timestamp()
                    WHERE settlement_intent_id = $1
                    """,
                    settlement_intent_id,
                )
                await connection.execute(
                    """
                    UPDATE arena402.pairings
                    SET status = 'settlement_failed',
                        completed_at = clock_timestamp()
                    WHERE pairing_id = $1
                    """,
                    row["pairing_id"],
                )
                await connection.execute(
                    """
                    UPDATE arena402.game_participants
                    SET status = 'active'
                    WHERE game_participant_id = ANY($1::text[])
                      AND status = 'settling'
                    """,
                    [
                        row["buyer_participant_id"],
                        row["seller_participant_id"],
                    ],
                )
                await self._event(
                    connection,
                    game_id=row["game_id"],
                    round_id=row["round_id"],
                    event_type="settlement.reverted",
                    source_key=f"{settlement_intent_id}:reverted",
                    public_payload={
                        "settlementIntentId": settlement_intent_id,
                        "txHash": normalized_tx,
                        "status": "reverted",
                    },
                )
                value = dict(row)
                value.update(
                    {
                        "status": "reverted",
                        "safe_error_code": "chain_transaction_reverted",
                        "block_number": None,
                        "block_hash": None,
                        "confirmation_count": None,
                    }
                )
                return self._settlement_public(value)

    async def commit_confirmed_inventory(
        self,
        *,
        settlement_intent_id: str,
    ) -> dict[str, object]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                intent = await connection.fetchrow(
                    """
                    SELECT *
                    FROM arena402.settlement_intents
                    WHERE settlement_intent_id = $1
                    FOR UPDATE
                    """,
                    settlement_intent_id,
                )
                if intent is None:
                    raise PawnhouseRepositoryError(
                        "settlement_intent_not_found"
                    )
                existing = await connection.fetchrow(
                    """
                    SELECT *
                    FROM arena402.inventory_commits
                    WHERE settlement_intent_id = $1
                    """,
                    settlement_intent_id,
                )
                if existing is not None:
                    return self._inventory_commit_public(
                        intent=intent,
                        commit=existing,
                    )
                if intent["status"] != "chain_confirmed_uncommitted":
                    raise PawnhouseRepositoryError(
                        "chain_confirmation_required"
                    )
                confirmed = await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM arena402.settlement_confirmations
                        WHERE settlement_intent_id = $1
                    )
                    """,
                    settlement_intent_id,
                )
                if not confirmed:
                    raise PawnhouseRepositoryError(
                        "chain_confirmation_required"
                    )
                participant_ids = sorted(
                    [
                        intent["buyer_participant_id"],
                        intent["seller_participant_id"],
                    ]
                )
                balance_rows = await connection.fetch(
                    """
                    SELECT game_participant_id, cash_atomic
                    FROM arena402.balances
                    WHERE game_participant_id = ANY($1::text[])
                    ORDER BY game_participant_id
                    FOR UPDATE
                    """,
                    participant_ids,
                )
                holding_rows = await connection.fetch(
                    """
                    SELECT game_participant_id, quantity
                    FROM arena402.holdings
                    WHERE game_participant_id = ANY($1::text[])
                      AND good_id = $2
                    ORDER BY game_participant_id
                    FOR UPDATE
                    """,
                    participant_ids,
                    intent["good_id"],
                )
                balances = {
                    row["game_participant_id"]: int(row["cash_atomic"])
                    for row in balance_rows
                }
                holdings = {
                    row["game_participant_id"]: int(row["quantity"])
                    for row in holding_rows
                }
                buyer_id = intent["buyer_participant_id"]
                seller_id = intent["seller_participant_id"]
                if set(balances) != {buyer_id, seller_id}:
                    raise PawnhouseRepositoryError(
                        "settlement_balance_projection_missing"
                    )
                if set(holdings) != {buyer_id, seller_id}:
                    raise PawnhouseRepositoryError(
                        "settlement_holding_projection_missing"
                    )
                amount = int(intent["amount_atomic"])
                buyer_cash_before = balances[buyer_id]
                seller_cash_before = balances[seller_id]
                buyer_holding_before = holdings[buyer_id]
                seller_holding_before = holdings[seller_id]
                if buyer_cash_before < amount:
                    raise PawnhouseRepositoryError(
                        "buyer_cash_changed_before_commit"
                    )
                if seller_holding_before < 1:
                    raise PawnhouseRepositoryError(
                        "seller_inventory_changed_before_commit"
                    )
                buyer_cash_after = buyer_cash_before - amount
                seller_cash_after = seller_cash_before + amount
                buyer_holding_after = buyer_holding_before + 1
                seller_holding_after = seller_holding_before - 1
                await connection.execute(
                    """
                    UPDATE arena402.balances
                    SET cash_atomic = (
                            CASE
                                WHEN game_participant_id = $1
                                THEN $3::numeric
                                WHEN game_participant_id = $2
                                THEN $4::numeric
                            END
                        ),
                        version = version + 1,
                        updated_at = clock_timestamp()
                    WHERE game_participant_id = ANY($5::text[])
                    """,
                    buyer_id,
                    seller_id,
                    buyer_cash_after,
                    seller_cash_after,
                    participant_ids,
                )
                await connection.execute(
                    """
                    UPDATE arena402.holdings
                    SET quantity = (
                            CASE
                                WHEN game_participant_id = $1
                                THEN $3::bigint
                                WHEN game_participant_id = $2
                                THEN $4::bigint
                            END
                        ),
                        version = version + 1,
                        updated_at = clock_timestamp()
                    WHERE game_participant_id = ANY($5::text[])
                      AND good_id = $6
                    """,
                    buyer_id,
                    seller_id,
                    buyer_holding_after,
                    seller_holding_after,
                    participant_ids,
                    intent["good_id"],
                )
                commit_id = f"inventory-commit:{settlement_intent_id}"
                commit = await connection.fetchrow(
                    """
                    INSERT INTO arena402.inventory_commits (
                        inventory_commit_id, settlement_intent_id,
                        buyer_cash_before_atomic, buyer_cash_after_atomic,
                        seller_cash_before_atomic, seller_cash_after_atomic,
                        buyer_holding_before, buyer_holding_after,
                        seller_holding_before, seller_holding_after
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10
                    )
                    RETURNING *
                    """,
                    commit_id,
                    settlement_intent_id,
                    buyer_cash_before,
                    buyer_cash_after,
                    seller_cash_before,
                    seller_cash_after,
                    buyer_holding_before,
                    buyer_holding_after,
                    seller_holding_before,
                    seller_holding_after,
                )
                await connection.execute(
                    """
                    UPDATE arena402.settlement_intents
                    SET status = 'inventory_committed',
                        inventory_committed_at = clock_timestamp(),
                        completed_at = clock_timestamp()
                    WHERE settlement_intent_id = $1
                    """,
                    settlement_intent_id,
                )
                await connection.execute(
                    """
                    UPDATE arena402.pairings
                    SET status = 'settled',
                        completed_at = clock_timestamp()
                    WHERE pairing_id = $1
                    """,
                    intent["pairing_id"],
                )
                await connection.execute(
                    """
                    UPDATE arena402.game_participants
                    SET status = 'active'
                    WHERE game_participant_id = ANY($1::text[])
                      AND status = 'settling'
                    """,
                    participant_ids,
                )
                await self._event(
                    connection,
                    game_id=intent["game_id"],
                    round_id=intent["round_id"],
                    event_type="settlement.inventory_committed",
                    source_key=f"{settlement_intent_id}:inventory_committed",
                    public_payload={
                        "settlementIntentId": settlement_intent_id,
                        "pairingId": intent["pairing_id"],
                        "good": intent["good_id"],
                        "quantity": int(intent["quantity"]),
                        "amountAtomic": str(intent["amount_atomic"]),
                        "status": "inventory_committed",
                    },
                )
                updated_intent = dict(intent)
                updated_intent["status"] = "inventory_committed"
                return self._inventory_commit_public(
                    intent=updated_intent,
                    commit=commit,
                )

    async def automatable_game_ids(self, *, limit: int = 50) -> list[str]:
        if limit < 1 or limit > 500:
            raise ValueError("automation game limit must be between 1 and 500")
        rows = await self._require_pool().fetch(
            """
            SELECT game_id
            FROM arena402.games
            WHERE phase = 'running'
            ORDER BY started_at, game_id
            LIMIT $1
            """,
            limit,
        )
        return [str(row["game_id"]) for row in rows]

    async def automation_state(
        self,
        *,
        game_id: str,
    ) -> dict[str, object]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            game = await connection.fetchrow(
                """
                SELECT phase, current_round
                FROM arena402.games
                WHERE game_id = $1
                """,
                game_id,
            )
            if game is None:
                raise PawnhouseRepositoryError("game_not_found")
            if game["phase"] != "running":
                return {"gameId": game_id, "action": "idle"}
            round_row = await connection.fetchrow(
                """
                SELECT round_id, round_index, phase
                FROM arena402.rounds
                WHERE game_id = $1
                  AND round_index = $2
                """,
                game_id,
                game["current_round"],
            )
            if round_row is None:
                raise PawnhouseRepositoryError("current_round_not_found")
            runtime_rows = await connection.fetch(
                """
                SELECT runtime_kind, count(*) AS participant_count
                FROM arena402.game_participants
                WHERE game_id = $1
                  AND status IN ('active', 'settling')
                GROUP BY runtime_kind
                ORDER BY runtime_kind
                """,
                game_id,
            )
            runtime_kinds = {
                str(row["runtime_kind"]): int(row["participant_count"])
                for row in runtime_rows
            }
            runtime_run = await connection.fetchrow(
                """
                SELECT status, stage, safe_error_code
                FROM arena402.runtime_runs
                WHERE round_id = $1
                  AND runtime_kind IN ('hosted', 'mixed')
                ORDER BY runtime_kind
                LIMIT 1
                """,
                round_row["round_id"],
            )
            active_negotiations = int(
                await connection.fetchval(
                    """
                    SELECT count(*)
                    FROM arena402.negotiations
                    WHERE round_id = $1
                      AND status = 'active'
                    """,
                    round_row["round_id"],
                )
            )
            pending_settlements = int(
                await connection.fetchval(
                    """
                    SELECT count(*)
                    FROM arena402.pairings
                    WHERE round_id = $1
                      AND status IN (
                          'accepted_pending_settlement',
                          'settling'
                      )
                    """,
                    round_row["round_id"],
                )
            )

        action = "idle"
        phase = str(round_row["phase"])
        task_runtime_game = bool(runtime_kinds) and set(
            runtime_kinds
        ).issubset({"hosted", "connector"})
        if phase == "decide":
            if set(runtime_kinds) == {"rule"}:
                action = "run_rule"
            elif task_runtime_game:
                if runtime_run is None:
                    action = "enqueue_agent_runtime"
                elif runtime_run["status"] == "failed":
                    action = "blocked_runtime_failure"
                else:
                    action = "wait_runtime"
            else:
                action = "wait_runtime_adapter"
        elif phase in {"negotiate", "settle", "round_close"}:
            if task_runtime_game and (
                runtime_run is None or runtime_run["status"] != "completed"
            ):
                action = (
                    "blocked_runtime_failure"
                    if runtime_run is not None
                    and runtime_run["status"] == "failed"
                    else "wait_runtime"
                )
            elif active_negotiations:
                action = "wait_negotiation"
            elif pending_settlements:
                action = "wait_settlement"
            else:
                action = "advance_round"
        return {
            "gameId": game_id,
            "roundId": str(round_row["round_id"]),
            "roundIndex": int(round_row["round_index"]),
            "roundPhase": phase,
            "runtimeKinds": runtime_kinds,
            "activeNegotiations": active_negotiations,
            "pendingSettlements": pending_settlements,
            "runtimeRunStatus": (
                None if runtime_run is None else str(runtime_run["status"])
            ),
            "runtimeRunStage": (
                None if runtime_run is None else str(runtime_run["stage"])
            ),
            "runtimeErrorCode": (
                None
                if runtime_run is None
                else runtime_run["safe_error_code"]
            ),
            "action": action,
        }

    async def advance_round_or_game(
        self,
        *,
        game_id: str,
    ) -> dict[str, object]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                game = await connection.fetchrow(
                    """
                    SELECT
                        phase, current_round, round_count, event_seed,
                        event_schedule_commitment
                    FROM arena402.games
                    WHERE game_id = $1
                    FOR UPDATE
                    """,
                    game_id,
                )
                if game is None:
                    raise PawnhouseRepositoryError("game_not_found")
                if game["phase"] == "completed":
                    return {
                        "gameId": game_id,
                        "transition": "already_completed",
                    }
                if game["phase"] != "running":
                    raise PawnhouseRepositoryError("game_not_running")
                round_row = await connection.fetchrow(
                    """
                    SELECT round_id, round_index, phase
                    FROM arena402.rounds
                    WHERE game_id = $1
                      AND round_index = $2
                    FOR UPDATE
                    """,
                    game_id,
                    game["current_round"],
                )
                if round_row is None:
                    raise PawnhouseRepositoryError(
                        "current_round_not_found"
                    )
                round_id = str(round_row["round_id"])
                phase = str(round_row["phase"])
                runtime_rows = await connection.fetch(
                    """
                    SELECT DISTINCT runtime_kind
                    FROM arena402.game_participants
                    WHERE game_id = $1
                      AND status IN ('active', 'settling')
                    """,
                    game_id,
                )
                runtime_kinds = {
                    str(value["runtime_kind"]) for value in runtime_rows
                }
                if runtime_kinds and runtime_kinds.issubset(
                    {"hosted", "connector"}
                ):
                    run_status = await connection.fetchval(
                        """
                        SELECT status
                        FROM arena402.runtime_runs
                        WHERE round_id = $1
                          AND runtime_kind IN ('hosted', 'mixed')
                        ORDER BY runtime_kind
                        LIMIT 1
                        """,
                        round_id,
                    )
                    if run_status != "completed":
                        return {
                            "gameId": game_id,
                            "roundId": round_id,
                            "transition": "waiting_runtime",
                        }

                active_negotiations = int(
                    await connection.fetchval(
                        """
                        SELECT count(*)
                        FROM arena402.negotiations
                        WHERE round_id = $1
                          AND status = 'active'
                        """,
                        round_id,
                    )
                )
                if active_negotiations:
                    return {
                        "gameId": game_id,
                        "roundId": round_id,
                        "transition": "waiting_negotiation",
                    }
                pending_settlements = int(
                    await connection.fetchval(
                        """
                        SELECT count(*)
                        FROM arena402.pairings
                        WHERE round_id = $1
                          AND status IN (
                              'accepted_pending_settlement',
                              'settling'
                          )
                        """,
                        round_id,
                    )
                )
                if pending_settlements:
                    if phase == "negotiate":
                        await connection.execute(
                            """
                            UPDATE arena402.rounds
                            SET phase = 'settle'
                            WHERE round_id = $1
                              AND phase = 'negotiate'
                            """,
                            round_id,
                        )
                        await connection.execute(
                            """
                            UPDATE public.rounds
                            SET phase = 'settling'
                            WHERE round_id = $1
                            """,
                            round_id,
                        )
                    return {
                        "gameId": game_id,
                        "roundId": round_id,
                        "transition": "waiting_settlement",
                        "pendingSettlements": pending_settlements,
                    }
                if phase in {"negotiate", "settle"}:
                    await connection.execute(
                        """
                        UPDATE arena402.rounds
                        SET phase = 'round_close',
                            phase_deadline_at = NULL
                        WHERE round_id = $1
                          AND phase IN ('negotiate', 'settle')
                        """,
                        round_id,
                    )
                    phase = "round_close"
                if phase != "round_close":
                    raise PawnhouseRepositoryError(
                        "round_not_ready_to_close"
                    )

                await self._snapshot_round_portfolios(
                    connection,
                    game_id=game_id,
                    round_id=round_id,
                    round_index=int(round_row["round_index"]),
                )
                await connection.execute(
                    """
                    UPDATE arena402.rounds
                    SET phase = 'completed',
                        phase_deadline_at = NULL,
                        completed_at = COALESCE(
                            completed_at,
                            clock_timestamp()
                        )
                    WHERE round_id = $1
                    """,
                    round_id,
                )
                await connection.execute(
                    """
                    UPDATE public.rounds
                    SET phase = 'completed',
                        deadline_at = NULL,
                        completed_at = COALESCE(
                            completed_at,
                            clock_timestamp()
                        )
                    WHERE round_id = $1
                    """,
                    round_id,
                )
                await self._event(
                    connection,
                    game_id=game_id,
                    round_id=round_id,
                    event_type="round.closed",
                    source_key=f"{round_id}:closed",
                    public_payload={
                        "roundId": round_id,
                        "roundIndex": int(round_row["round_index"]),
                    },
                )

                if int(game["current_round"]) < int(game["round_count"]):
                    next_round = int(game["current_round"]) + 1
                    result = await self._open_next_round(
                        connection,
                        game_id=game_id,
                        round_index=next_round,
                    )
                    return {
                        "gameId": game_id,
                        "transition": "next_round",
                        **result,
                    }

                rankings = await self._finalize_game(
                    connection,
                    game_id=game_id,
                    round_index=int(round_row["round_index"]),
                    event_seed=str(game["event_seed"]),
                    event_schedule_commitment=str(
                        game["event_schedule_commitment"]
                    ),
                )
                return {
                    "gameId": game_id,
                    "roundId": round_id,
                    "transition": "game_completed",
                    "rankings": rankings,
                }

    async def inventory_commit_for_intent(
        self,
        *,
        settlement_intent_id: str,
    ) -> dict[str, object]:
        row = await self._require_pool().fetchrow(
            """
            SELECT
                i.status,
                c.*
            FROM arena402.settlement_intents AS i
            JOIN arena402.inventory_commits AS c
              ON c.settlement_intent_id = i.settlement_intent_id
            WHERE i.settlement_intent_id = $1
            """,
            settlement_intent_id,
        )
        if row is None:
            raise PawnhouseRepositoryError("inventory_commit_not_found")
        return self._inventory_commit_public(
            intent=row,
            commit=row,
        )

    async def current_game(
        self,
        *,
        owner_user_id: str | None = None,
    ) -> dict[str, object]:
        """Return the public product projection for the explicit Current Game."""

        pool = self._require_pool()
        game = await pool.fetchrow(
            """
            SELECT
                g.game_id,
                g.phase,
                pointer.start_threshold,
                pointer.max_participants,
                g.round_count,
                g.current_round,
                active_round.phase AS round_phase,
                g.created_at,
                g.started_at,
                g.completed_at
            FROM arena402.current_game AS pointer
            JOIN arena402.games AS g
              ON g.game_id = pointer.game_id
            LEFT JOIN LATERAL (
                SELECT round_row.phase
                FROM arena402.rounds AS round_row
                WHERE round_row.game_id = g.game_id
                ORDER BY round_row.round_index DESC
                LIMIT 1
            ) AS active_round ON TRUE
            WHERE pointer.singleton
              AND g.phase <> 'cancelled'
            """
        )
        if game is None:
            raise PawnhouseRepositoryError("current_game_not_found")

        participants = await pool.fetch(
            """
            SELECT
                participant.game_participant_id,
                participant.agent_id,
                coalesce(agent.name, participant.agent_id) AS display_name,
                participant.runtime_kind,
                participant.joined_at
            FROM arena402.game_participants AS participant
            LEFT JOIN public.arena_agents AS agent
              ON agent.agent_id = participant.agent_id
            WHERE participant.game_id = $1
              AND participant.status <> 'cancelled'
            ORDER BY participant.joined_at, participant.game_participant_id
            """,
            game["game_id"],
        )
        joined_by_me = False
        if owner_user_id is not None:
            joined_by_me = bool(
                await pool.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM arena402.game_participants
                        WHERE game_id = $1
                          AND user_id = $2
                          AND status <> 'cancelled'
                    )
                    """,
                    game["game_id"],
                    owner_user_id,
                )
            )

        phase = str(game["phase"])
        if phase in {"registration", "portfolio_setup", "portfolio_locked"}:
            status = "WAITING"
        elif phase in {"running", "final_valuation"}:
            status = "RUNNING"
        elif phase == "completed":
            status = "COMPLETED"
        else:
            raise PawnhouseRepositoryError("current_game_not_found")

        # Existing participation rows predate the v2 Join authorization and
        # mandate checks. Keep them visible but fail closed until that workflow
        # records an explicit Ready projection.
        public_participants = [
            {
                "participantId": str(row["game_participant_id"]),
                "agentId": str(row["agent_id"]),
                "displayName": str(row["display_name"]),
                "runtimeKind": str(row["runtime_kind"]),
                "readiness": "PENDING",
                "joinedAt": row["joined_at"].isoformat(),
            }
            for row in participants
        ]
        return {
            "game": {
                "gameId": str(game["game_id"]),
                "status": status,
                "readyCount": 0,
                "startThreshold": int(game["start_threshold"]),
                "maxParticipants": int(game["max_participants"]),
                "roundCount": int(game["round_count"]),
                "currentRound": int(game["current_round"]),
                "roundPhase": (
                    str(game["round_phase"])
                    if game["round_phase"] is not None
                    else None
                ),
                "joinedByMe": joined_by_me,
                "participants": public_participants,
                "createdAt": game["created_at"].isoformat(),
                "startedAt": (
                    game["started_at"].isoformat()
                    if game["started_at"] is not None
                    else None
                ),
                "completedAt": (
                    game["completed_at"].isoformat()
                    if game["completed_at"] is not None
                    else None
                ),
            },
            "nextGamePending": status == "COMPLETED",
            "schemaVersion": "arena.current-game.v1",
        }

    async def game_state(self, game_id: str) -> dict[str, object]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            game = await connection.fetchrow(
                """
                SELECT
                    game_id, phase, round_count, current_round,
                    event_schedule_commitment, event_seed,
                    event_seed_revealed_at, started_at, completed_at
                FROM arena402.games
                WHERE game_id = $1
                """,
                game_id,
            )
            if game is None:
                raise PawnhouseRepositoryError("game_not_found")
            participants = await connection.fetch(
                """
                SELECT
                    game_participant_id, agent_id, runtime_kind, status
                FROM arena402.game_participants
                WHERE game_id = $1
                ORDER BY joined_at, game_participant_id
                """,
                game_id,
            )
            rounds = await connection.fetch(
                """
                SELECT round_id, round_index, phase, phase_deadline_at
                FROM arena402.rounds
                WHERE game_id = $1
                ORDER BY round_index
                """,
                game_id,
            )
            rankings = await connection.fetch(
                """
                SELECT
                    r.rank, r.game_participant_id, p.agent_id,
                    r.net_worth_atomic, r.tier, r.calculated_at
                FROM arena402.rankings AS r
                JOIN arena402.game_participants AS p
                  ON p.game_participant_id = r.game_participant_id
                WHERE r.game_id = $1
                ORDER BY r.rank
                """,
                game_id,
            )
            final_prices = await connection.fetch(
                """
                SELECT good_id, price_atomic, source_round_index, frozen_at
                FROM arena402.final_settlement_prices
                WHERE game_id = $1
                ORDER BY good_id
                """,
                game_id,
            )
        return {
            "gameId": game["game_id"],
            "phase": game["phase"],
            "roundCount": game["round_count"],
            "currentRound": game["current_round"],
            "eventScheduleCommitment": game["event_schedule_commitment"],
            "eventSeed": (
                str(game["event_seed"])
                if game["event_seed_revealed_at"] is not None
                else None
            ),
            "participants": [dict(row) for row in participants],
            "rounds": [dict(row) for row in rounds],
            "finalPrices": {
                str(row["good_id"]): str(int(row["price_atomic"]))
                for row in final_prices
            },
            "rankings": [
                {
                    "rank": int(row["rank"]),
                    "participantId": str(row["game_participant_id"]),
                    "agentId": str(row["agent_id"]),
                    "netWorthAtomic": str(int(row["net_worth_atomic"])),
                    "tier": str(row["tier"]),
                    "calculatedAt": row["calculated_at"].isoformat(),
                }
                for row in rankings
            ],
            "schemaVersion": "arena.pawnhouse-game-state.v1",
        }

    async def timeline(
        self,
        game_id: str,
        *,
        after_sequence: int = 0,
    ) -> list[dict[str, object]]:
        pool = self._require_pool()
        rows = await pool.fetch(
            """
            SELECT
                event_sequence, round_id, event_type, public_payload,
                created_at
            FROM arena402.game_events
            WHERE game_id = $1
              AND event_sequence > $2
            ORDER BY event_sequence
            LIMIT 1000
            """,
            game_id,
            after_sequence,
        )
        return [
            {
                "sequence": row["event_sequence"],
                "roundId": row["round_id"],
                "type": row["event_type"],
                "data": (
                    json.loads(row["public_payload"])
                    if isinstance(row["public_payload"], str)
                    else dict(row["public_payload"])
                ),
                "createdAt": row["created_at"].isoformat(),
            }
            for row in rows
        ]

    async def _pair_locked_round(
        self,
        connection: Any,
        *,
        game_id: str,
        round_id: str,
    ) -> tuple[Pairing, ...]:
        rows = await connection.fetch(
            """
            SELECT
                pool_entry_id, game_id, round_id, game_participant_id,
                side, good_id, result_received_at
            FROM arena402.pool_entries
            WHERE round_id = $1
              AND status = 'unmatched'
            ORDER BY result_received_at, pool_entry_id
            FOR UPDATE
            """,
            round_id,
        )
        entries = tuple(
            PoolEntry(
                pool_entry_id=row["pool_entry_id"],
                game_id=row["game_id"],
                round_id=row["round_id"],
                participant_id=row["game_participant_id"],
                side=row["side"],
                good=row["good_id"],
                entered_at=row["result_received_at"],
            )
            for row in rows
        )
        pairings = fcfs_pair(entries)
        timeout_ms = await connection.fetchval(
            """
            SELECT action_timeout_ms
            FROM arena402.games
            WHERE game_id = $1
            """,
            game_id,
        )
        for pairing in pairings:
            await connection.execute(
                """
                INSERT INTO arena402.pairings (
                    pairing_id, game_id, round_id, good_id,
                    buyer_entry_id, seller_entry_id,
                    buyer_participant_id, seller_participant_id,
                    pairing_sequence
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                pairing.pairing_id,
                pairing.game_id,
                pairing.round_id,
                pairing.good,
                pairing.buyer_entry_id,
                pairing.seller_entry_id,
                pairing.buyer_participant_id,
                pairing.seller_participant_id,
                pairing.sequence,
            )
            await connection.execute(
                """
                UPDATE arena402.pool_entries
                SET status = 'paired'
                WHERE pool_entry_id = ANY($1::text[])
                """,
                [pairing.buyer_entry_id, pairing.seller_entry_id],
            )
            negotiation_id = f"neg:{pairing.pairing_id}"
            await connection.execute(
                """
                INSERT INTO arena402.negotiations (
                    negotiation_id, pairing_id, game_id, round_id,
                    buyer_participant_id, seller_participant_id, max_turns,
                    action_deadline_at
                )
                SELECT
                    $1, $2, $3, $4, $5, $6,
                    max_negotiation_turns,
                    clock_timestamp() + $7 * interval '1 millisecond'
                FROM arena402.games
                WHERE game_id = $3
                """,
                negotiation_id,
                pairing.pairing_id,
                pairing.game_id,
                pairing.round_id,
                pairing.buyer_participant_id,
                pairing.seller_participant_id,
                timeout_ms,
            )
            await self._event(
                connection,
                game_id=game_id,
                round_id=round_id,
                event_type="pairing.created",
                source_key=f"{pairing.pairing_id}:created",
                public_payload=self._pairing_public(pairing),
            )
        return pairings

    async def _run_rule_negotiations(
        self,
        connection: Any,
        *,
        game_id: str,
        round_id: str,
    ) -> list[dict[str, object]]:
        rows = await connection.fetch(
            """
            SELECT *
            FROM arena402.negotiations
            WHERE game_id = $1
              AND round_id = $2
              AND status = 'active'
            ORDER BY created_at, negotiation_id
            FOR UPDATE
            """,
            game_id,
            round_id,
        )
        outputs: list[dict[str, object]] = []
        for row in rows:
            negotiation = Negotiation(
                negotiation_id=row["negotiation_id"],
                buyer_participant_id=row["buyer_participant_id"],
                seller_participant_id=row["seller_participant_id"],
                max_turns=row["max_turns"],
            )
            config_rows = await connection.fetch(
                """
                SELECT
                    p.game_participant_id, c.intent, c.good_id,
                    c.target_price_atomic, c.public_message
                FROM arena402.game_participants AS p
                JOIN arena402.rule_runtime_configs AS c
                  ON c.game_participant_id = p.game_participant_id
                WHERE p.game_participant_id = ANY($1::text[])
                """,
                [
                    negotiation.buyer_participant_id,
                    negotiation.seller_participant_id,
                ],
            )
            runtimes = {
                config["game_participant_id"]: RuleRuntime(
                    RuleStrategy(
                        intent=config["intent"],
                        good=config["good_id"],
                        target_price_atomic=int(config["target_price_atomic"]),
                        public_message=config["public_message"],
                    )
                )
                for config in config_rows
            }
            while negotiation.status is NegotiationStatus.ACTIVE:
                role = negotiation.next_role
                participant_id = (
                    negotiation.buyer_participant_id
                    if role == "buyer"
                    else negotiation.seller_participant_id
                )
                runtime = runtimes.get(participant_id)
                if runtime is None:
                    raise PawnhouseRepositoryError(
                        "non_rule_negotiation_not_supported_in_milestone_2"
                    )
                previous_price = (
                    negotiation.turns[-1].action.price_atomic
                    if negotiation.turns
                    and negotiation.turns[-1].action.action == "propose"
                    else None
                )
                sequence = len(negotiation.turns) + 1
                action = runtime.negotiate(
                    role=role,
                    sequence=sequence,
                    latest_counterparty_price_atomic=previous_price,
                    max_turns=negotiation.max_turns,
                )
                turn = negotiation.apply(role=role, action=action)
                source_result_id = (
                    f"rule-neg-result:{negotiation.negotiation_id}:"
                    f"{turn.sequence}"
                )
                await connection.execute(
                    """
                    INSERT INTO arena402.negotiation_messages (
                        negotiation_message_id, negotiation_id, game_id,
                        round_id, source_result_id, turn_sequence, actor_role,
                        action, price_atomic, public_message
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    f"msg:{negotiation.negotiation_id}:{turn.sequence}",
                    negotiation.negotiation_id,
                    game_id,
                    round_id,
                    source_result_id,
                    turn.sequence,
                    turn.role,
                    turn.action.action,
                    turn.action.price_atomic,
                    turn.action.message,
                )
                await self._event(
                    connection,
                    game_id=game_id,
                    round_id=round_id,
                    event_type="negotiation.message",
                    source_key=source_result_id,
                    public_payload={
                        "negotiationId": negotiation.negotiation_id,
                        "turn": turn.sequence,
                        "role": turn.role,
                        "action": turn.action.action,
                        "priceAtomic": (
                            str(turn.action.price_atomic)
                            if turn.action.price_atomic is not None
                            else None
                        ),
                        "message": turn.action.message,
                    },
                )
            completed_at = datetime.now(timezone.utc)
            await connection.execute(
                """
                UPDATE arena402.negotiations
                SET turn_count = $2,
                    next_role = 'none',
                    status = $3,
                    latest_proposal_price_atomic = $4,
                    latest_proposal_role = $5,
                    accepted_price_atomic = $6,
                    completed_at = $7
                WHERE negotiation_id = $1
                """,
                negotiation.negotiation_id,
                len(negotiation.turns),
                negotiation.status.value,
                next(
                    (
                        turn.action.price_atomic
                        for turn in reversed(negotiation.turns)
                        if turn.action.action == "propose"
                    ),
                    None,
                ),
                next(
                    (
                        turn.role
                        for turn in reversed(negotiation.turns)
                        if turn.action.action == "propose"
                    ),
                    None,
                ),
                negotiation.accepted_price_atomic,
                completed_at,
            )
            pairing_status = negotiation.status.value
            await connection.execute(
                """
                UPDATE arena402.pairings
                SET status = $2::text,
                    completed_at = (
                        CASE
                            WHEN $2::text IN ('rejected', 'timeout')
                            THEN $3::timestamptz
                            ELSE NULL::timestamptz
                        END
                    )
                WHERE pairing_id = (
                    SELECT pairing_id
                    FROM arena402.negotiations
                    WHERE negotiation_id = $1
                )
                """,
                negotiation.negotiation_id,
                pairing_status,
                completed_at,
            )
            if (
                negotiation.status
                is NegotiationStatus.ACCEPTED_PENDING_SETTLEMENT
            ):
                await self._freeze_settlement_intent(
                    connection,
                    negotiation_id=negotiation.negotiation_id,
                )
            outputs.append(
                {
                    "negotiationId": negotiation.negotiation_id,
                    "status": negotiation.status.value,
                    "acceptedPriceAtomic": (
                        str(negotiation.accepted_price_atomic)
                        if negotiation.accepted_price_atomic is not None
                        else None
                    ),
                    "turnCount": len(negotiation.turns),
                }
            )
        return outputs

    async def _freeze_settlement_intent(
        self,
        connection: Any,
        *,
        negotiation_id: str,
    ) -> SettlementIntent | None:
        row = await connection.fetchrow(
            """
            SELECT
                n.negotiation_id,
                n.pairing_id,
                n.game_id,
                n.round_id,
                n.buyer_participant_id,
                n.seller_participant_id,
                n.accepted_price_atomic,
                p.good_id,
                g.config_snapshot,
                buyer.agent_id AS buyer_agent_id,
                seller.agent_id AS seller_agent_id,
                buyer_account.chain_id AS buyer_chain_id,
                buyer_account.account_address AS buyer_account,
                seller_account.chain_id AS seller_chain_id,
                seller_account.account_address AS seller_account,
                buyer_balance.cash_atomic AS buyer_cash_atomic,
                seller_holding.quantity AS seller_quantity
            FROM arena402.negotiations AS n
            JOIN arena402.pairings AS p
              ON p.pairing_id = n.pairing_id
             AND p.game_id = n.game_id
             AND p.round_id = n.round_id
            JOIN arena402.games AS g
              ON g.game_id = n.game_id
            JOIN arena402.game_participants AS buyer
              ON buyer.game_participant_id = n.buyer_participant_id
             AND buyer.game_id = n.game_id
            JOIN arena402.game_participants AS seller
              ON seller.game_participant_id = n.seller_participant_id
             AND seller.game_id = n.game_id
            LEFT JOIN arena402.participant_settlement_accounts AS buyer_account
              ON buyer_account.game_participant_id =
                 n.buyer_participant_id
            LEFT JOIN arena402.participant_settlement_accounts AS seller_account
              ON seller_account.game_participant_id =
                 n.seller_participant_id
            JOIN arena402.balances AS buyer_balance
              ON buyer_balance.game_participant_id =
                 n.buyer_participant_id
            JOIN arena402.holdings AS seller_holding
              ON seller_holding.game_participant_id =
                 n.seller_participant_id
             AND seller_holding.good_id = p.good_id
            WHERE n.negotiation_id = $1
            FOR SHARE OF n, p, g, buyer, seller,
                buyer_balance, seller_holding
            """,
            negotiation_id,
        )
        if row is None:
            raise PawnhouseRepositoryError("negotiation_not_found")
        game_config = (
            json.loads(row["config_snapshot"])
            if isinstance(row["config_snapshot"], str)
            else dict(row["config_snapshot"])
        )
        settlement_config = self._settlement_config(game_config)
        if settlement_config.authorization_mode == "none":
            return None
        if row["accepted_price_atomic"] is None:
            raise PawnhouseRepositoryError(
                "accepted_negotiation_has_no_price"
            )
        if row["buyer_account"] is None or row["seller_account"] is None:
            raise PawnhouseRepositoryError("settlement_account_missing")
        assert settlement_config.chain_id is not None
        assert settlement_config.token_address is not None
        assert settlement_config.token_symbol is not None
        assert settlement_config.token_decimals is not None
        if (
            int(row["buyer_chain_id"]) != settlement_config.chain_id
            or int(row["seller_chain_id"]) != settlement_config.chain_id
        ):
            raise PawnhouseRepositoryError(
                "settlement_account_chain_mismatch"
            )
        accepted_price = int(row["accepted_price_atomic"])
        if int(row["buyer_cash_atomic"]) < accepted_price:
            raise PawnhouseRepositoryError(
                "buyer_has_insufficient_cash_for_settlement"
            )
        if int(row["seller_quantity"]) < 1:
            raise PawnhouseRepositoryError(
                "seller_has_no_inventory_for_settlement"
            )
        intent_id = f"settlement:{negotiation_id}"
        intent = SettlementIntent(
            settlement_intent_id=intent_id,
            game_id=row["game_id"],
            round_id=row["round_id"],
            pairing_id=row["pairing_id"],
            negotiation_id=row["negotiation_id"],
            buyer_participant_id=row["buyer_participant_id"],
            seller_participant_id=row["seller_participant_id"],
            buyer_agent_id=row["buyer_agent_id"],
            seller_agent_id=row["seller_agent_id"],
            buyer_account=row["buyer_account"],
            seller_account=row["seller_account"],
            good=row["good_id"],
            quantity=1,
            unit_price_atomic=accepted_price,
            amount_atomic=accepted_price,
            chain_id=settlement_config.chain_id,
            token_address=settlement_config.token_address,
            token_symbol=settlement_config.token_symbol,
            token_decimals=settlement_config.token_decimals,
            token_eip712_name=settlement_config.token_eip712_name,
            token_eip712_version=settlement_config.token_eip712_version,
            required_confirmations=(
                settlement_config.required_confirmations
            ),
            authorization_mode="single_eip3009",
            idempotency_key=(
                f"{row['game_id']}:{row['round_id']}:"
                f"{row['negotiation_id']}"
            ),
        )
        inserted = await connection.fetchrow(
            """
            INSERT INTO arena402.settlement_intents (
                settlement_intent_id, game_id, round_id, pairing_id,
                negotiation_id, buyer_participant_id,
                seller_participant_id, buyer_agent_id, seller_agent_id,
                buyer_account, seller_account, good_id, quantity,
                unit_price_atomic, amount_atomic, chain_id, token_address,
                token_symbol, token_decimals, token_eip712_name,
                token_eip712_version, required_confirmations,
                authorization_mode, idempotency_key, intent_snapshot,
                intent_hash
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                $13, $14, $15, $16, $17, $18, $19, $20, $21, $22,
                $23, $24, $25, $26::jsonb, $27
            )
            ON CONFLICT (negotiation_id) DO NOTHING
            RETURNING settlement_intent_id
            """,
            intent.settlement_intent_id,
            intent.game_id,
            intent.round_id,
            intent.pairing_id,
            intent.negotiation_id,
            intent.buyer_participant_id,
            intent.seller_participant_id,
            intent.buyer_agent_id,
            intent.seller_agent_id,
            intent.buyer_account,
            intent.seller_account,
            intent.good,
            intent.quantity,
            intent.unit_price_atomic,
            intent.amount_atomic,
            intent.chain_id,
            intent.token_address,
            intent.token_symbol,
            intent.token_decimals,
            intent.token_eip712_name,
            intent.token_eip712_version,
            intent.required_confirmations,
            intent.authorization_mode,
            intent.idempotency_key,
            _json(intent.to_snapshot()),
            intent.intent_hash,
        )
        if inserted is None:
            existing = await connection.fetchrow(
                """
                SELECT intent_hash
                FROM arena402.settlement_intents
                WHERE negotiation_id = $1
                """,
                negotiation_id,
            )
            if (
                existing is None
                or existing["intent_hash"] != intent.intent_hash
            ):
                raise PawnhouseRepositoryError(
                    "settlement_intent_conflict"
                )
            return intent
        await connection.execute(
            """
            UPDATE arena402.game_participants
            SET status = 'settling'
            WHERE game_participant_id = ANY($1::text[])
              AND status = 'active'
            """,
            [
                intent.buyer_participant_id,
                intent.seller_participant_id,
            ],
        )
        await connection.execute(
            """
            UPDATE arena402.pairings
            SET status = 'settling'
            WHERE pairing_id = $1
              AND status = 'accepted_pending_settlement'
            """,
            intent.pairing_id,
        )
        await connection.execute(
            """
            UPDATE arena402.rounds
            SET phase = 'settle',
                phase_deadline_at = NULL
            WHERE round_id = $1
              AND phase = 'negotiate'
            """,
            intent.round_id,
        )
        await self._event(
            connection,
            game_id=intent.game_id,
            round_id=intent.round_id,
            event_type="settlement.intent_frozen",
            source_key=f"{intent.settlement_intent_id}:frozen",
            public_payload={
                "settlementIntentId": intent.settlement_intent_id,
                "pairingId": intent.pairing_id,
                "negotiationId": intent.negotiation_id,
                "good": intent.good,
                "quantity": intent.quantity,
                "amountAtomic": str(intent.amount_atomic),
                "chainId": intent.chain_id,
                "tokenAddress": intent.token_address,
                "sellerAccount": intent.seller_account,
                "authorizationMode": intent.authorization_mode,
                "status": "authorization_requested",
            },
        )
        return intent

    @staticmethod
    def _settlement_config(
        game_config: Mapping[str, object],
    ) -> SettlementConfig:
        raw = game_config.get("settlement")
        if raw is None:
            return SettlementConfig()
        if not isinstance(raw, Mapping):
            raise PawnhouseRepositoryError("invalid_settlement_config")
        try:
            mode = str(raw.get("authorizationMode", "none"))
            if mode == "none":
                return SettlementConfig()
            return SettlementConfig(
                authorization_mode=mode,  # type: ignore[arg-type]
                chain_id=int(raw["chainId"]),
                token_address=str(raw["tokenAddress"]),
                token_symbol=str(raw["tokenSymbol"]),
                token_decimals=int(raw["tokenDecimals"]),
                token_eip712_name=str(raw["tokenEip712Name"]),
                token_eip712_version=str(raw["tokenEip712Version"]),
                required_confirmations=int(
                    raw.get("requiredConfirmations", 1)
                ),
            )
        except (KeyError, TypeError, ValueError, SettlementError):
            raise PawnhouseRepositoryError(
                "invalid_settlement_config"
            ) from None

    @staticmethod
    def _intent_from_row(row: Mapping[str, object]) -> SettlementIntent:
        intent = SettlementIntent(
            settlement_intent_id=str(row["settlement_intent_id"]),
            game_id=str(row["game_id"]),
            round_id=str(row["round_id"]),
            pairing_id=str(row["pairing_id"]),
            negotiation_id=str(row["negotiation_id"]),
            buyer_participant_id=str(row["buyer_participant_id"]),
            seller_participant_id=str(row["seller_participant_id"]),
            buyer_agent_id=str(row["buyer_agent_id"]),
            seller_agent_id=str(row["seller_agent_id"]),
            buyer_account=str(row["buyer_account"]),
            seller_account=str(row["seller_account"]),
            good=str(row["good_id"]),
            quantity=int(row["quantity"]),
            unit_price_atomic=int(row["unit_price_atomic"]),
            amount_atomic=int(row["amount_atomic"]),
            chain_id=int(row["chain_id"]),
            token_address=str(row["token_address"]),
            token_symbol=str(row["token_symbol"]),
            token_decimals=int(row["token_decimals"]),
            required_confirmations=int(row["required_confirmations"]),
            authorization_mode=str(  # type: ignore[arg-type]
                row["authorization_mode"]
            ),
            idempotency_key=str(row["idempotency_key"]),
            token_eip712_name=(
                str(row["token_eip712_name"])
                if row.get("token_eip712_name") is not None
                else None
            ),
            token_eip712_version=(
                str(row["token_eip712_version"])
                if row.get("token_eip712_version") is not None
                else None
            ),
        )
        if intent.intent_hash != row["intent_hash"]:
            raise PawnhouseRepositoryError(
                "settlement_intent_integrity_failure"
            )
        return intent

    @staticmethod
    def _settlement_public(
        row: Mapping[str, object],
    ) -> dict[str, object]:
        raw_snapshot = row["intent_snapshot"]
        snapshot = (
            json.loads(raw_snapshot)
            if isinstance(raw_snapshot, str)
            else dict(raw_snapshot)
        )
        intent = PostgresPawnhouseRepository._intent_from_row(row)
        if snapshot != intent.to_snapshot():
            raise PawnhouseRepositoryError(
                "settlement_intent_snapshot_integrity_failure"
            )
        snapshot.update(
            {
                "intentHash": intent.intent_hash,
                "approvalRecorded": row.get("approval_source") is not None,
                "approvalSource": row.get("approval_source"),
                "status": row["status"],
                "safeErrorCode": row.get("safe_error_code"),
                "txHash": row.get("tx_hash"),
                "submissionSource": row.get("submission_source"),
                "blockNumber": (
                    int(row["block_number"])
                    if row.get("block_number") is not None
                    else None
                ),
                "blockHash": row.get("block_hash"),
                "confirmationCount": row.get("confirmation_count"),
                "createdAt": (
                    row["created_at"].isoformat()
                    if hasattr(row["created_at"], "isoformat")
                    else row["created_at"]
                ),
            }
        )
        return snapshot

    @staticmethod
    def _inventory_commit_public(
        *,
        intent: Mapping[str, object],
        commit: Mapping[str, object],
    ) -> dict[str, object]:
        return {
            "settlementIntentId": intent["settlement_intent_id"],
            "status": "inventory_committed",
            "inventoryCommitId": commit["inventory_commit_id"],
            "buyerCashBeforeAtomic": str(
                int(commit["buyer_cash_before_atomic"])
            ),
            "buyerCashAfterAtomic": str(
                int(commit["buyer_cash_after_atomic"])
            ),
            "sellerCashBeforeAtomic": str(
                int(commit["seller_cash_before_atomic"])
            ),
            "sellerCashAfterAtomic": str(
                int(commit["seller_cash_after_atomic"])
            ),
            "buyerHoldingBefore": int(commit["buyer_holding_before"]),
            "buyerHoldingAfter": int(commit["buyer_holding_after"]),
            "sellerHoldingBefore": int(commit["seller_holding_before"]),
            "sellerHoldingAfter": int(commit["seller_holding_after"]),
            "committedAt": (
                commit["committed_at"].isoformat()
                if hasattr(commit["committed_at"], "isoformat")
                else commit["committed_at"]
            ),
            "schemaVersion": "arena402.inventory-commit.v1",
        }

    async def _snapshot_round_portfolios(
        self,
        connection: Any,
        *,
        game_id: str,
        round_id: str,
        round_index: int,
    ) -> None:
        rows = await connection.fetch(
            """
            SELECT
                p.game_participant_id,
                b.cash_atomic,
                h.good_id,
                h.quantity
            FROM arena402.game_participants AS p
            JOIN arena402.balances AS b
              ON b.game_participant_id = p.game_participant_id
            JOIN arena402.holdings AS h
              ON h.game_participant_id = p.game_participant_id
             AND h.game_id = p.game_id
            WHERE p.game_id = $1
            ORDER BY p.game_participant_id, h.good_id
            """,
            game_id,
        )
        portfolios: dict[str, dict[str, object]] = {}
        for row in rows:
            participant_id = str(row["game_participant_id"])
            value = portfolios.setdefault(
                participant_id,
                {
                    "cash_atomic": int(row["cash_atomic"]),
                    "holdings": {},
                },
            )
            holdings = value["holdings"]
            assert isinstance(holdings, dict)
            holdings[str(row["good_id"])] = int(row["quantity"])
        for participant_id, value in portfolios.items():
            await connection.execute(
                """
                INSERT INTO arena402.round_portfolio_snapshots (
                    game_id, round_id, round_index, game_participant_id,
                    cash_atomic, holdings_snapshot
                )
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                ON CONFLICT (round_id, game_participant_id) DO NOTHING
                """,
                game_id,
                round_id,
                round_index,
                participant_id,
                value["cash_atomic"],
                _json(value["holdings"]),
            )

    @staticmethod
    async def _scheduled_events(
        connection: Any,
        *,
        game_id: str,
    ) -> tuple[WorldEvent, ...]:
        rows = await connection.fetch(
            """
            SELECT
                round_index, event_id, display_name, narrative,
                duration_rounds, effect_snapshot, schema_version
            FROM arena402.event_schedule
            WHERE game_id = $1
            ORDER BY round_index
            """,
            game_id,
        )
        events: list[WorldEvent] = []
        for row in rows:
            raw_effects = row["effect_snapshot"]
            if isinstance(raw_effects, str):
                raw_effects = json.loads(raw_effects)
            effects: list[EventEffect] = []
            for raw in raw_effects:
                effect = dict(raw)
                effects.append(
                    EventEffect(
                        kind=EffectKind(str(effect["kind"])),
                        good=require_good(str(effect["good"])),
                        target=str(effect.get("target", "market")),
                        basis_points=(
                            int(effect["basisPoints"])
                            if effect.get("basisPoints") is not None
                            else None
                        ),
                        order_price_atomic=(
                            int(effect["orderPriceAtomic"])
                            if effect.get("orderPriceAtomic") is not None
                            else None
                        ),
                        order_limit=(
                            int(effect["orderLimit"])
                            if effect.get("orderLimit") is not None
                            else None
                        ),
                    )
                )
            events.append(
                WorldEvent(
                    event_id=str(row["event_id"]),
                    display_name=str(row["display_name"]),
                    narrative=str(row["narrative"]),
                    reveal_round=int(row["round_index"]),
                    duration_rounds=(
                        int(row["duration_rounds"])
                        if row["duration_rounds"] is not None
                        else None
                    ),
                    effects=tuple(effects),
                    schema_version=str(row["schema_version"]),
                )
            )
        return tuple(events)

    async def _open_next_round(
        self,
        connection: Any,
        *,
        game_id: str,
        round_index: int,
    ) -> dict[str, object]:
        events = await self._scheduled_events(
            connection,
            game_id=game_id,
        )
        event_by_round = {event.reveal_round: event for event in events}
        event = event_by_round.get(round_index)
        if event is None:
            raise PawnhouseRepositoryError("round_event_not_found")
        world = WorldState({value.event_id: value for value in events})
        snapshot = None
        for scheduled in events:
            if scheduled.reveal_round > round_index:
                break
            snapshot = world.reveal(
                scheduled.event_id,
                round_index=scheduled.reveal_round,
            )
        if snapshot is None:
            raise PawnhouseRepositoryError("round_world_snapshot_missing")

        round_id = f"round:{game_id}:{round_index}"
        await connection.execute(
            """
            INSERT INTO arena402.rounds (
                round_id, game_id, round_index, phase
            )
            VALUES ($1, $2, $3, 'event_reveal')
            ON CONFLICT (game_id, round_index) DO NOTHING
            """,
            round_id,
            game_id,
            round_index,
        )
        await connection.execute(
            """
            UPDATE arena402.games
            SET current_round = $2
            WHERE game_id = $1
              AND phase = 'running'
            """,
            game_id,
            round_index,
        )
        await self._event(
            connection,
            game_id=game_id,
            round_id=round_id,
            event_type="round.started",
            source_key=f"{round_id}:started",
            public_payload={
                "roundId": round_id,
                "roundIndex": round_index,
            },
        )
        await self._persist_world_snapshot(
            connection,
            game_id=game_id,
            round_id=round_id,
            event=event,
            snapshot=snapshot,
        )
        deadline_at = await connection.fetchval(
            """
            UPDATE arena402.rounds
            SET phase = 'decide',
                phase_deadline_at = (
                    clock_timestamp()
                    + (
                        SELECT action_timeout_ms
                        FROM arena402.games
                        WHERE game_id = $2
                    ) * interval '1 millisecond'
                )
            WHERE round_id = $1
            RETURNING phase_deadline_at
            """,
            round_id,
            game_id,
        )
        await connection.execute(
            """
            INSERT INTO public.rounds (
                round_id, game_id, round_index, phase, deadline_at
            )
            VALUES ($1, $2, $3, 'decide', $4)
            ON CONFLICT (game_id, round_index) DO NOTHING
            """,
            round_id,
            game_id,
            round_index,
            deadline_at,
        )
        return {
            "roundId": round_id,
            "roundIndex": round_index,
            "roundPhase": "decide",
            "deadlineAt": deadline_at,
            "eventId": event.event_id,
        }

    async def _finalize_game(
        self,
        connection: Any,
        *,
        game_id: str,
        round_index: int,
        event_seed: str,
        event_schedule_commitment: str,
    ) -> list[dict[str, object]]:
        await connection.execute(
            """
            INSERT INTO arena402.final_settlement_prices (
                game_id, good_id, price_atomic, source_round_index
            )
            SELECT
                game_id, good_id, final_price_atomic, round_index
            FROM arena402.price_snapshots
            WHERE game_id = $1
              AND round_index = $2
            ON CONFLICT (game_id, good_id) DO NOTHING
            """,
            game_id,
            round_index,
        )
        final_rows = await connection.fetch(
            """
            SELECT good_id, price_atomic
            FROM arena402.final_settlement_prices
            WHERE game_id = $1
            ORDER BY good_id
            """,
            game_id,
        )
        final_prices = {
            require_good(str(row["good_id"])): int(row["price_atomic"])
            for row in final_rows
        }
        if set(final_prices) != set(GOOD_IDS):
            raise PawnhouseRepositoryError(
                "final_settlement_prices_incomplete"
            )

        portfolio_rows = await connection.fetch(
            """
            SELECT
                p.game_participant_id,
                p.agent_id,
                b.cash_atomic,
                h.good_id,
                h.quantity
            FROM arena402.game_participants AS p
            JOIN arena402.balances AS b
              ON b.game_participant_id = p.game_participant_id
            JOIN arena402.holdings AS h
              ON h.game_participant_id = p.game_participant_id
             AND h.game_id = p.game_id
            WHERE p.game_id = $1
            ORDER BY p.game_participant_id, h.good_id
            """,
            game_id,
        )
        portfolios: dict[str, Portfolio] = {}
        participant_agents: dict[str, str] = {}
        mutable: dict[str, dict[str, object]] = {}
        for row in portfolio_rows:
            participant_id = str(row["game_participant_id"])
            participant_agents[participant_id] = str(row["agent_id"])
            value = mutable.setdefault(
                participant_id,
                {
                    "cash_atomic": int(row["cash_atomic"]),
                    "holdings": {},
                },
            )
            holdings = value["holdings"]
            assert isinstance(holdings, dict)
            holdings[str(row["good_id"])] = int(row["quantity"])
        for participant_id, value in mutable.items():
            portfolios[participant_id] = Portfolio(
                cash_atomic=int(value["cash_atomic"]),
                holdings=dict(value["holdings"]),
            )

        entries = calculate_rankings(portfolios, final_prices)
        public_rankings: list[dict[str, object]] = []
        for entry in entries:
            await connection.execute(
                """
                INSERT INTO arena402.rankings (
                    game_id, game_participant_id, rank,
                    net_worth_atomic, tier
                )
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (game_id, game_participant_id) DO NOTHING
                """,
                game_id,
                entry.agent_id,
                entry.rank,
                entry.net_worth_atomic,
                entry.tier,
            )
            public_rankings.append(
                {
                    "rank": entry.rank,
                    "participantId": entry.agent_id,
                    "agentId": participant_agents[entry.agent_id],
                    "netWorthAtomic": str(entry.net_worth_atomic),
                    "tier": entry.tier,
                }
            )

        await connection.execute(
            """
            UPDATE arena402.games
            SET phase = 'completed',
                completed_at = COALESCE(
                    completed_at,
                    clock_timestamp()
                ),
                event_seed_revealed_at = COALESCE(
                    event_seed_revealed_at,
                    clock_timestamp()
                )
            WHERE game_id = $1
            """,
            game_id,
        )
        await connection.execute(
            """
            UPDATE arena402.game_participants
            SET status = 'completed',
                completed_at = COALESCE(
                    completed_at,
                    clock_timestamp()
                )
            WHERE game_id = $1
            """,
            game_id,
        )
        await connection.execute(
            """
            UPDATE public.games
            SET status = 'completed',
                completed_at = COALESCE(
                    completed_at,
                    clock_timestamp()
                )
            WHERE game_id = $1
            """,
            game_id,
        )
        await connection.execute(
            """
            UPDATE public.game_agents
            SET status = 'completed',
                completed_at = COALESCE(
                    completed_at,
                    clock_timestamp()
                )
            WHERE game_id = $1
            """,
            game_id,
        )
        await self._event(
            connection,
            game_id=game_id,
            event_type="game.completed",
            source_key=f"{game_id}:completed",
            public_payload={
                "roundCount": round_index,
                "eventSeed": event_seed,
                "eventScheduleCommitment": event_schedule_commitment,
                "finalPricesAtomic": {
                    good_id: str(final_prices[good_id])
                    for good_id in GOOD_IDS
                },
                "rankings": public_rankings,
            },
        )
        return public_rankings

    async def _persist_world_snapshot(
        self,
        connection: Any,
        *,
        game_id: str,
        round_id: str,
        event: WorldEvent,
        snapshot: Any,
    ) -> None:
        public_event = {
            "eventId": event.event_id,
            "displayName": event.display_name,
            "narrative": event.narrative,
            "round": event.reveal_round,
            "effects": [effect.to_wire() for effect in event.effects],
        }
        await connection.execute(
            """
            INSERT INTO arena402.event_occurrences (
                game_id, round_index, event_id, public_snapshot
            )
            VALUES ($1, $2, $3, $4::jsonb)
            """,
            game_id,
            event.reveal_round,
            event.event_id,
            _json(public_event),
        )
        for good_id in GOOD_IDS:
            await connection.execute(
                """
                INSERT INTO arena402.price_snapshots (
                    game_id, round_index, good_id, market_price_atomic,
                    final_price_atomic, supply_index_bps,
                    bubble_premium_bps
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                game_id,
                event.reveal_round,
                good_id,
                snapshot.market_prices[good_id],
                snapshot.final_prices[good_id],
                snapshot.supply_index_bps[good_id],
                snapshot.bubble_premium_bps[good_id],
            )
        for order in snapshot.royal_orders:
            if order.event_id != event.event_id:
                continue
            await connection.execute(
                """
                INSERT INTO arena402.royal_orders (
                    royal_order_id, game_id, round_id, source_event_id,
                    side, good_id, price_atomic, quantity_limit
                )
                VALUES ($1, $2, $3, $4, 'buy', $5, $6, $7)
                """,
                f"royal:{game_id}:{event.event_id}:{order.good}",
                game_id,
                round_id,
                event.event_id,
                order.good,
                order.price_atomic,
                order.quantity_limit,
            )
        await self._event(
            connection,
            game_id=game_id,
            round_id=round_id,
            event_type="world.event_revealed",
            source_key=f"{game_id}:{event.event_id}:revealed",
            public_payload=public_event,
        )

    @staticmethod
    async def _event(
        connection: Any,
        *,
        game_id: str,
        event_type: str,
        source_key: str,
        public_payload: Mapping[str, object],
        round_id: str | None = None,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO arena402.game_events (
                game_id, round_id, event_type, public_payload,
                source_idempotency_key
            )
            VALUES ($1, $2, $3, $4::jsonb, $5)
            ON CONFLICT (game_id, source_idempotency_key) DO NOTHING
            """,
            game_id,
            round_id,
            event_type,
            _json(public_payload),
            source_key,
        )

    @staticmethod
    def _pairing_public(pairing: Pairing) -> dict[str, object]:
        return {
            "pairingId": pairing.pairing_id,
            "good": pairing.good,
            "buyerParticipantId": pairing.buyer_participant_id,
            "sellerParticipantId": pairing.seller_participant_id,
            "sequence": pairing.sequence,
        }


__all__ = [
    "PawnhouseRepositoryError",
    "PostgresPawnhouseRepository",
]
