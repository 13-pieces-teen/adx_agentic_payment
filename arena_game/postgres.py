"""PostgreSQL repository for the clean-slate King's Pawnhouse vertical slice."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from arena_core.hashing import sha256_identifier

from .events import WorldEvent, WorldState, schedule_commitment
from .goods import GOODS, GOOD_IDS
from .market import Pairing, PoolEntry, fcfs_pair
from .money import gold
from .negotiation import Negotiation, NegotiationAction, NegotiationStatus
from .portfolio import Portfolio
from .rule_runtime import RuleRuntime, RuleStrategy


class PawnhouseRepositoryError(RuntimeError):
    pass


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
        action_timeout_ms: int = 90_000,
        max_negotiation_turns: int = 3,
    ) -> dict[str, object]:
        if not game_id:
            raise PawnhouseRepositoryError("game_id_required")
        commitment = schedule_commitment(events, seed=event_seed)
        config = {
            "world": "aurelia-402",
            "venue": "kings-pawnhouse",
            "roundCount": len(events),
            "initialNetWorthAtomic": "20000000",
            "fixedTradeQuantity": 1,
            "goldScale": 1_000_000,
            "schemaVersion": "arena.pawnhouse-game.v1",
        }
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                inserted = await connection.fetchval(
                    """
                    INSERT INTO arena402.games (
                        game_id, round_count, action_timeout_ms,
                        max_negotiation_turns, config_snapshot, event_seed,
                        event_schedule_commitment
                    )
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                    ON CONFLICT (game_id) DO NOTHING
                    RETURNING game_id
                    """,
                    game_id,
                    len(events),
                    action_timeout_ms,
                    max_negotiation_turns,
                    _json(config),
                    event_seed,
                    commitment,
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
                phase = await connection.fetchval(
                    """
                    SELECT phase
                    FROM arena402.games
                    WHERE game_id = $1
                    FOR UPDATE
                    """,
                    game_id,
                )
                if phase not in ("registration", "portfolio_setup"):
                    raise PawnhouseRepositoryError("game_not_joinable")
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
    ) -> str:
        participant_id = f"gp:{game_id}:{agent_id}"
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                phase = await connection.fetchval(
                    """
                    SELECT phase
                    FROM arena402.games
                    WHERE game_id = $1
                    FOR UPDATE
                    """,
                    game_id,
                )
                if phase is None:
                    raise PawnhouseRepositoryError("game_not_found")
                if phase not in ("registration", "portfolio_setup"):
                    raise PawnhouseRepositoryError("game_not_joinable")
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

    async def start_game(
        self,
        *,
        game_id: str,
        events: tuple[WorldEvent, ...],
    ) -> dict[str, object]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                game = await connection.fetchrow(
                    """
                    SELECT phase, min_participants, round_count
                    FROM arena402.games
                    WHERE game_id = $1
                    FOR UPDATE
                    """,
                    game_id,
                )
                if game is None:
                    raise PawnhouseRepositoryError("game_not_found")
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

    async def enqueue_hosted_run(self, *, game_id: str) -> dict[str, object]:
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
                            WHERE runtime_kind = 'hosted'
                        ) AS hosted
                    FROM arena402.game_participants
                    WHERE game_id = $1
                      AND status = 'active'
                    """,
                    game_id,
                )
                if counts["total"] < 2 or counts["total"] != counts["hosted"]:
                    raise PawnhouseRepositoryError(
                        "hosted_run_requires_only_hosted_participants"
                    )
                run_id = f"hosted-run:{round_row['round_id']}"
                await connection.execute(
                    """
                    INSERT INTO arena402.runtime_runs (
                        runtime_run_id, game_id, round_id, runtime_kind
                    )
                    VALUES ($1, $2, $3, 'hosted')
                    ON CONFLICT (round_id, runtime_kind) DO NOTHING
                    """,
                    run_id,
                    game_id,
                    round_row["round_id"],
                )
                await self._event(
                    connection,
                    game_id=game_id,
                    round_id=round_row["round_id"],
                    event_type="runtime.run_queued",
                    source_key=f"{run_id}:queued",
                    public_payload={
                        "runtimeRunId": run_id,
                        "runtimeKind": "hosted",
                    },
                )
        return {
            "gameId": game_id,
            "roundId": round_row["round_id"],
            "runtimeRunId": run_id,
            "status": "queued",
        }

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
                WHERE runtime_kind = 'hosted'
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
                  AND p.runtime_kind = 'hosted'
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

    async def game_state(self, game_id: str) -> dict[str, object]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            game = await connection.fetchrow(
                """
                SELECT
                    game_id, phase, round_count, current_round,
                    event_schedule_commitment, started_at, completed_at
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
        return {
            "gameId": game["game_id"],
            "phase": game["phase"],
            "roundCount": game["round_count"],
            "currentRound": game["current_round"],
            "eventScheduleCommitment": game["event_schedule_commitment"],
            "participants": [dict(row) for row in participants],
            "rounds": [dict(row) for row in rounds],
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
