"""PostgreSQL repository for the clean-slate King's Pawnhouse vertical slice."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .events import WorldEvent, WorldState, schedule_commitment
from .goods import GOODS, GOOD_IDS
from .market import Pairing, PoolEntry, fcfs_pair
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
