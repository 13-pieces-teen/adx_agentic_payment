"""PostgreSQL repository for the clean-slate King's Pawnhouse vertical slice."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from pydantic import TypeAdapter, ValidationError

from arena_agent_contracts import (
    ArenaMarketIntentInputV1,
    ArenaMarketRfqInputV1,
    ArenaMarketSelectInputV1,
    BuyAction,
    EngageRequestActionV1,
    MarketIntentActionV1,
    MarketRfqActionV1,
    MarketSelectionActionV1,
    PassAction,
    RejectAllRequestsActionV1,
    RequestNegotiationsActionV1,
    SellAction,
)
from arena_core.hashing import sha256_identifier, sha256_text_identifier
from arena_core.candidate_validation import (
    market_intent_candidate_violation,
    market_rfq_candidate_violation,
    market_select_candidate_violation,
)
from arena_core.models import AppliedArenaAction
from db_pool_config import api_pool_max_size

from .a2a_market import NEGOTIATE_STAGE_ACTION_SLOTS
from .events import (
    EffectKind,
    EventEffect,
    MARKET_FEEDBACK_POLICY_VERSION_V1,
    WorldEvent,
    WorldState,
    apply_market_feedback,
    schedule_commitment,
)
from .goods import GOODS, GOOD_IDS, INITIAL_PRICES, GoodId, require_good
from .liquidity import (
    LiquidityIntent,
    MarketSide,
    summarize_round_liquidity,
)
from .market import Pairing, PoolEntry, fcfs_pair
from .money import gold
from .negotiation import Negotiation, NegotiationAction, NegotiationStatus
from .official_filler import official_seat_deficit
from .portfolio import (
    INITIAL_NET_WORTH_ATOMIC,
    Portfolio,
    default_join_portfolio,
    distribute_balanced_portfolios,
)
from .price_catalog import (
    STANDARD_PRICE_CATALOG_ID,
    price_catalog_from_snapshot,
    resolve_price_catalog,
)
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
CURRENT_GAME_START_THRESHOLD = 10
CURRENT_GAME_MAX_PARTICIPANTS = 100

_MARKET_INTENT_ACTION_ADAPTER = TypeAdapter(MarketIntentActionV1)
_MARKET_RFQ_ACTION_ADAPTER = TypeAdapter(MarketRfqActionV1)
_MARKET_SELECT_ACTION_ADAPTER = TypeAdapter(MarketSelectionActionV1)


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _should_assign_balanced_portfolios(config: Mapping[str, object]) -> bool:
    """Keep legacy managed Current Games from overwriting Join-time choices."""

    return (
        config.get("portfolioMode") == "balanced_auto"
        and config.get("currentGameManaged") is not True
    )


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class PostgresPawnhouseRepository:
    def __init__(
        self,
        database_url: str,
        *,
        pool: Any | None = None,
        database_role: str = "adx_arena_core",
    ) -> None:
        if database_role not in {"adx_arena_core", "adx_settlement"}:
            raise ValueError("invalid_pawnhouse_database_role")
        self.database_url = database_url
        self.database_role = database_role
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
                min_size=0,
                max_size=api_pool_max_size(),
                command_timeout=30,
                setup=self._setup_connection,
            )
            self._owns_pool = True

    async def close(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
            self._pool = None

    async def _setup_connection(self, connection: Any) -> None:
        await connection.execute(f"SET ROLE {self.database_role}")
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
        price_catalog_id: str = STANDARD_PRICE_CATALOG_ID,
        action_timeout_ms: int = 90_000,
        max_negotiation_turns: int = 3,
        min_participants: int = 2,
        max_participants: int = 16,
        portfolio_mode: str = "manual",
        market_protocol: str = "fcfs.v1",
        settlement_config: SettlementConfig | None = None,
        operator_user_id: str | None = None,
    ) -> dict[str, object]:
        if not game_id:
            raise PawnhouseRepositoryError("game_id_required")
        if min_participants < 2:
            raise PawnhouseRepositoryError("invalid_min_participants")
        if max_participants < 2:
            raise PawnhouseRepositoryError("invalid_max_participants")
        if max_participants < min_participants:
            raise PawnhouseRepositoryError("invalid_participant_range")
        if portfolio_mode not in {"manual", "balanced_auto"}:
            raise PawnhouseRepositoryError("invalid_portfolio_mode")
        if market_protocol not in {"fcfs.v1", "agent_a2a.v1"}:
            raise PawnhouseRepositoryError("invalid_market_protocol")
        try:
            price_catalog = resolve_price_catalog(price_catalog_id)
        except ValueError:
            raise PawnhouseRepositoryError(
                "invalid_price_catalog"
            ) from None
        commitment = schedule_commitment(events, seed=event_seed)
        resolved_settlement = settlement_config or SettlementConfig()
        config = {
            "world": "aurelia-402",
            "venue": "kings-pawnhouse",
            "roundCount": len(events),
            "minParticipants": min_participants,
            "maxParticipants": max_participants,
            "portfolioMode": portfolio_mode,
            "eventDeckId": event_deck_id,
            "eventDeckVersion": 1,
            "eventMode": event_mode,
            **price_catalog.to_snapshot(),
            "marketFeedbackPolicyVersion": (
                MARKET_FEEDBACK_POLICY_VERSION_V1
            ),
            "initialNetWorthAtomic": "20000000",
            "initial_cash_atomic": 20_000_000,
            "initial_inventory": {
                "grain": 0,
                "iron": 0,
                "warhorse": 0,
                "gems": 0,
            },
            "fixedTradeQuantity": 1,
            "marketProtocol": market_protocol,
            "goldScale": 1_000_000,
            "settlement": resolved_settlement.to_snapshot(),
            "schemaVersion": "arena.pawnhouse-game.v1",
        }
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._insert_game(
                    connection,
                    game_id=game_id,
                    events=events,
                    event_seed=event_seed,
                    commitment=commitment,
                    action_timeout_ms=action_timeout_ms,
                    max_negotiation_turns=max_negotiation_turns,
                    min_participants=min_participants,
                    max_participants=max_participants,
                    market_protocol=market_protocol,
                    config=config,
                    operator_user_id=operator_user_id,
                    claim_current=market_protocol == "fcfs.v1",
                )
        return {
            "gameId": game_id,
            "phase": "registration",
            "marketProtocol": market_protocol,
            "eventScheduleCommitment": commitment,
        }

    async def ensure_current_game(
        self,
        *,
        game_id: str,
        events: tuple[WorldEvent, ...],
        event_seed: str,
        event_deck_id: str = "pawnhouse-standard-v1",
        event_mode: str = "seeded_shuffle",
        price_catalog_id: str = STANDARD_PRICE_CATALOG_ID,
        action_timeout_ms: int = 90_000,
        max_negotiation_turns: int = 3,
        start_threshold: int = CURRENT_GAME_START_THRESHOLD,
        max_participants: int = CURRENT_GAME_MAX_PARTICIPANTS,
        official_fill_after_seconds: int = 300,
        settlement_config: SettlementConfig | None = None,
        market_protocol: str = "fcfs.v1",
    ) -> dict[str, object]:
        """Atomically keep one joinable/running product Game authoritative."""

        if not game_id:
            raise PawnhouseRepositoryError("game_id_required")
        if not 2 <= start_threshold <= CURRENT_GAME_MAX_PARTICIPANTS:
            raise PawnhouseRepositoryError("invalid_start_threshold")
        if not (
            start_threshold
            <= max_participants
            <= CURRENT_GAME_MAX_PARTICIPANTS
        ):
            raise PawnhouseRepositoryError("invalid_max_participants")
        if official_fill_after_seconds <= 0:
            raise PawnhouseRepositoryError(
                "invalid_official_fill_after_seconds"
            )
        if market_protocol not in {"fcfs.v1", "agent_a2a.v1"}:
            raise PawnhouseRepositoryError("invalid_market_protocol")
        try:
            price_catalog = resolve_price_catalog(price_catalog_id)
        except ValueError:
            raise PawnhouseRepositoryError(
                "invalid_price_catalog"
            ) from None

        commitment = schedule_commitment(events, seed=event_seed)
        resolved_settlement = settlement_config or SettlementConfig()
        config = {
            "world": "aurelia-402",
            "venue": "kings-pawnhouse",
            "roundCount": len(events),
            "minParticipants": start_threshold,
            "maxParticipants": max_participants,
            "officialFillAfterSeconds": official_fill_after_seconds,
            "officialAgentSelectionVersion": "arena.official-selection.v1",
            "officialAgentStrategyCatalogVersion": (
                "arena.hosted-strategy.v1"
            ),
            "portfolioMode": "manual",
            "eventDeckId": event_deck_id,
            "eventDeckVersion": 1,
            "eventMode": event_mode,
            **price_catalog.to_snapshot(),
            "marketFeedbackPolicyVersion": (
                MARKET_FEEDBACK_POLICY_VERSION_V1
            ),
            "initialNetWorthAtomic": "20000000",
            "initial_cash_atomic": 20_000_000,
            "initial_inventory": {
                "grain": 0,
                "iron": 0,
                "warhorse": 0,
                "gems": 0,
            },
            "fixedTradeQuantity": 1,
            "marketProtocol": market_protocol,
            "goldScale": 1_000_000,
            "settlement": resolved_settlement.to_snapshot(),
            "currentGameManaged": True,
            "schemaVersion": "arena.pawnhouse-game.v1",
        }

        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.fetchval(
                    """
                    SELECT pg_advisory_xact_lock(
                        hashtext('arena402.current-game-lifecycle')
                    )
                    """
                )
                current = await connection.fetchrow(
                    """
                    SELECT pointer.game_id, game.phase
                    FROM arena402.current_game AS pointer
                    JOIN arena402.games AS game
                      ON game.game_id = pointer.game_id
                    WHERE pointer.singleton = TRUE
                    FOR UPDATE OF pointer, game
                    """
                )
                if current is not None and current["phase"] not in {
                    "completed",
                    "cancelled",
                }:
                    return {
                        "gameId": str(current["game_id"]),
                        "created": False,
                    }

                previous_game_id = (
                    None if current is None else str(current["game_id"])
                )
                await self._insert_game(
                    connection,
                    game_id=game_id,
                    events=events,
                    event_seed=event_seed,
                    commitment=commitment,
                    action_timeout_ms=action_timeout_ms,
                    max_negotiation_turns=max_negotiation_turns,
                    min_participants=start_threshold,
                    max_participants=max_participants,
                    market_protocol=market_protocol,
                    config=config,
                    operator_user_id=None,
                    claim_current=False,
                )
                await connection.execute(
                    """
                    INSERT INTO arena402.current_game (
                        singleton, game_id, start_threshold, max_participants
                    )
                    VALUES (TRUE, $1, $2, $3)
                    ON CONFLICT (singleton) DO UPDATE
                    SET game_id = EXCLUDED.game_id,
                        start_threshold = EXCLUDED.start_threshold,
                        max_participants = EXCLUDED.max_participants,
                        updated_at = clock_timestamp()
                    """,
                    game_id,
                    start_threshold,
                    max_participants,
                )
                return {
                    "gameId": game_id,
                    "created": True,
                    "previousGameId": previous_game_id,
                    "eventScheduleCommitment": commitment,
                }

    async def _insert_game(
        self,
        connection: Any,
        *,
        game_id: str,
        events: tuple[WorldEvent, ...],
        event_seed: str,
        commitment: str,
        action_timeout_ms: int,
        max_negotiation_turns: int,
        min_participants: int,
        max_participants: int,
        market_protocol: str,
        config: Mapping[str, object],
        operator_user_id: str | None,
        claim_current: bool,
    ) -> None:
        inserted = await connection.fetchval(
            """
            INSERT INTO arena402.games (
                game_id, round_count, action_timeout_ms,
                max_negotiation_turns, min_participants, max_participants,
                config_snapshot, event_seed, event_schedule_commitment,
                operator_user_id, market_protocol
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11
            )
            ON CONFLICT (game_id) DO NOTHING
            RETURNING game_id
            """,
            game_id,
            len(events),
            action_timeout_ms,
            max_negotiation_turns,
            min_participants,
            max_participants,
            _json(config),
            event_seed,
            commitment,
            operator_user_id,
            market_protocol,
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
        if claim_current:
            await connection.execute(
                """
                INSERT INTO arena402.current_game (
                    singleton, game_id, start_threshold, max_participants
                )
                SELECT TRUE, game_id, min_participants, max_participants
                FROM arena402.games
                WHERE game_id = $1
                  AND min_participants BETWEEN 2 AND 100
                  AND max_participants BETWEEN min_participants AND 100
                ON CONFLICT (singleton) DO NOTHING
                """,
                game_id,
            )
        for good in GOODS.values():
            await connection.execute(
                """
                INSERT INTO arena402.game_goods (
                    game_id, good_id, display_name, initial_price_atomic
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
        portfolio: Portfolio | None,
        strategy: RuleStrategy,
    ) -> str:
        participant_id = f"gp:{game_id}:{agent_id}"
        portfolio = portfolio or default_join_portfolio(
            game_id=game_id,
            agent_id=agent_id,
        )
        # The binding is a game-join snapshot. The same logical Agent may join
        # later games without sharing mutable strategy/configuration state.
        runtime_binding_id = f"rule:{participant_id}"
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                phase = await connection.fetchrow(
                    """
                    SELECT phase, max_participants, market_protocol
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
                if phase["market_protocol"] == "agent_a2a.v1":
                    raise PawnhouseRepositoryError(
                        "agent_market_requires_agent_runtime"
                    )
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
        portfolio: Portfolio | None,
        settlement_account: SettlementAccount | None = None,
        payment_mandate_id: str | None = None,
        join_authorization_id: str | None = None,
        require_current_game: bool = False,
        official_pool_join: bool = False,
    ) -> str:
        participant_id = f"gp:{game_id}:{agent_id}"
        portfolio = portfolio or default_join_portfolio(
            game_id=game_id,
            agent_id=agent_id,
        )
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                phase = await connection.fetchrow(
                    """
                    SELECT phase, config_snapshot, max_participants,
                           min_participants
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
                existing_participant = await connection.fetchrow(
                    """
                    SELECT game_participant_id, agent_id, readiness
                    FROM arena402.game_participants
                    WHERE game_id = $1 AND user_id = $2
                    FOR SHARE
                    """,
                    game_id,
                    user_id,
                )
                if existing_participant is not None:
                    if (
                        existing_participant["agent_id"] == agent_id
                        and existing_participant["readiness"] != "withdrawn"
                    ):
                        return str(existing_participant["game_participant_id"])
                    raise PawnhouseRepositoryError("user_already_joined")
                official = None
                if official_pool_join:
                    official = await connection.fetchrow(
                        """
                        SELECT inventory.wallet_id, inventory.chain_id,
                               inventory.account_address,
                               pool_entry.strategy_archetype,
                               pool_entry.strategy_catalog_version
                        FROM arena402.official_agent_pool AS pool_entry
                        JOIN public.arena_agents AS agent
                          ON agent.agent_id = pool_entry.agent_id
                        JOIN arena402.wallet_inventory AS inventory
                          ON inventory.wallet_id = pool_entry.wallet_id
                        WHERE pool_entry.agent_id = $1
                          AND agent.owner_user_id = $2
                          AND pool_entry.enabled
                          AND inventory.status <> 'disabled'
                        FOR SHARE OF pool_entry, inventory
                        """,
                        agent_id,
                        user_id,
                    )
                    if official is None:
                        raise PawnhouseRepositoryError(
                            "official_agent_not_ready"
                        )
                    if payment_mandate_id is not None:
                        raise PawnhouseRepositoryError(
                            "official_mandate_not_allowed"
                        )
                    settlement_account = SettlementAccount(
                        chain_id=int(official["chain_id"]),
                        address=str(official["account_address"]),
                        custody_mode="sandbox_guest",
                    )
                    already_joined = await connection.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM arena402.game_participants
                            WHERE game_id = $1 AND agent_id = $2
                              AND readiness <> 'withdrawn'
                        )
                        """,
                        game_id,
                        agent_id,
                    )
                    if already_joined:
                        return participant_id
                participant_count = int(
                    await connection.fetchval(
                        """
                        SELECT count(*)
                        FROM arena402.game_participants
                        WHERE game_id = $1
                          AND readiness <> 'withdrawn'
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
                requires_game_coin_provision = (
                    settlement_config.authorization_mode != "none"
                    and settlement_config.token_symbol.lower() == "arena402-g"
                )
                if (
                    official_pool_join
                    and settlement_config.authorization_mode != "none"
                ):
                    assert official is not None
                    assert settlement_config.chain_id is not None
                    assert settlement_config.token_address is not None
                    now = datetime.now(timezone.utc)
                    mandate_expires_at = now + timedelta(hours=24)
                    official_join_authorization_id = (
                        f"official-ja:{participant_id}"
                    )
                    official_mandate_id = (
                        f"official-mandate:{participant_id}"
                    )
                    mandate_max_per_payment = int(
                        game_config.get("initialNetWorthAtomic", "20000000")
                    )
                    mandate_max_cumulative = (
                        mandate_max_per_payment
                        * int(game_config.get("roundCount", 5))
                    )
                    key_digest = sha256_text_identifier(
                        f"official-join:{participant_id}"
                    )
                    request_digest = sha256_identifier(
                        {
                            "gameId": game_id,
                            "agentId": agent_id,
                            "walletId": str(official["wallet_id"]),
                            "chainId": settlement_config.chain_id,
                            "tokenAddress": settlement_config.token_address,
                            "maxPerPaymentAtomic": mandate_max_per_payment,
                            "maxCumulativeAtomic": mandate_max_cumulative,
                        }
                    )
                    await connection.execute(
                        """
                        INSERT INTO arena402.join_authorizations (
                            join_authorization_id, user_id, game_id, agent_id,
                            status, key_digest, request_digest, expires_at
                        )
                        VALUES (
                            $1, $2, $3, $4, 'pending', $5, $6, $7
                        )
                        ON CONFLICT (join_authorization_id) DO NOTHING
                        """,
                        official_join_authorization_id,
                        user_id,
                        game_id,
                        agent_id,
                        key_digest,
                        request_digest,
                        mandate_expires_at,
                    )
                    await connection.execute(
                        """
                        INSERT INTO arena402.payment_mandates (
                            mandate_id, user_id, wallet_id, game_id, chain_id,
                            token_address, max_per_payment_atomic,
                            max_cumulative_atomic, allowed_payees, valid_from,
                            expires_at, join_authorization_id,
                            allowed_payee_rule
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8,
                            ARRAY[]::text[], $9, $10, $11,
                            'same_game_settlement_account'
                        )
                        ON CONFLICT (mandate_id) DO NOTHING
                        """,
                        official_mandate_id,
                        user_id,
                        str(official["wallet_id"]),
                        game_id,
                        settlement_config.chain_id,
                        settlement_config.token_address,
                        mandate_max_per_payment,
                        mandate_max_cumulative,
                        now - timedelta(seconds=5),
                        mandate_expires_at,
                        official_join_authorization_id,
                    )
                    payment_mandate_id = official_mandate_id
                    join_authorization_id = (
                        official_join_authorization_id
                    )
                if (
                    payment_mandate_id is not None
                    and settlement_account is None
                    and settlement_config.authorization_mode != "none"
                ):
                    wallet = await connection.fetchrow(
                        """
                        SELECT chain_id, account_address
                        FROM arena402.user_wallets
                        WHERE user_id = $1
                        FOR SHARE
                        """,
                        user_id,
                    )
                    if wallet is None:
                        raise PawnhouseRepositoryError("wallet_not_ready")
                    settlement_account = SettlementAccount(
                        chain_id=int(wallet["chain_id"]),
                        address=str(wallet["account_address"]),
                        custody_mode="sandbox_guest",
                    )
                if require_current_game:
                    pointer = await connection.fetchval(
                        """
                        SELECT game_id FROM arena402.current_game
                        WHERE singleton = TRUE FOR SHARE
                        """
                    )
                    if pointer != game_id:
                        raise PawnhouseRepositoryError("game_not_current")
                if settlement_config.authorization_mode != "none":
                    if settlement_account is None:
                        raise PawnhouseRepositoryError(
                            "settlement_account_required"
                        )
                    if settlement_account.chain_id != settlement_config.chain_id:
                        raise PawnhouseRepositoryError(
                            "settlement_account_chain_mismatch"
                        )
                if payment_mandate_id is not None:
                    mandate = await connection.fetchrow(
                        """
                        SELECT mandate.mandate_id
                        FROM arena402.payment_mandates AS mandate
                        JOIN arena402.join_authorizations AS join_auth
                          ON join_auth.join_authorization_id =
                             mandate.join_authorization_id
                        WHERE mandate.mandate_id = $1
                          AND mandate.user_id = $2
                          AND mandate.game_id = $3
                          AND mandate.revoked_at IS NULL
                          AND mandate.valid_from <= clock_timestamp()
                          AND mandate.expires_at > clock_timestamp()
                          AND mandate.allowed_payee_rule =
                              'same_game_settlement_account'
                          AND join_auth.join_authorization_id = $4
                          AND join_auth.user_id = $2
                          AND join_auth.game_id = $3
                          AND join_auth.agent_id = $5
                          AND join_auth.status = 'pending'
                          AND join_auth.expires_at > clock_timestamp()
                        FOR UPDATE OF mandate, join_auth
                        """,
                        payment_mandate_id,
                        user_id,
                        game_id,
                        join_authorization_id,
                        agent_id,
                    )
                    if mandate is None:
                        raise PawnhouseRepositoryError("mandate_not_ready")
                elif require_current_game and not official_pool_join:
                    raise PawnhouseRepositoryError("mandate_not_ready")
                hosted = await connection.fetchrow(
                    """
                    SELECT
                        a.agent_id,
                        a.name AS display_name,
                        b.runtime_binding_id,
                        hc.credential_id,
                        hc.provider,
                        hc.model,
                        hc.thinking_enabled,
                        strategy.instructions AS strategy_instructions,
                        hc.max_output_tokens,
                        hc.prompt_version,
                        hc.task_schema_version,
                        hc.action_schema_version,
                        hc.capability_version,
                        hc.adapter_version,
                        strategy.strategy_revision_id,
                        strategy.revision_no AS strategy_revision_no,
                        strategy.archetype AS strategy_archetype,
                        strategy.catalog_version AS strategy_catalog_version
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
                    JOIN public.hosted_agent_strategy_revisions AS strategy
                      ON strategy.agent_id = a.agent_id
                     AND strategy.status = 'active'
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
                    "display_name": str(
                        hosted.get("display_name") or agent_id
                    ),
                    "provider_id": hosted["provider"],
                    "model_id": hosted["model"],
                    "credential_id": hosted["credential_id"],
                    "thinking_enabled": hosted["thinking_enabled"],
                    "strategy_instructions": hosted["strategy_instructions"],
                    "strategy_revision_id": hosted[
                        "strategy_revision_id"
                    ],
                    "strategy_revision_no": hosted[
                        "strategy_revision_no"
                    ],
                    "strategy_archetype": hosted["strategy_archetype"],
                    "strategy_catalog_version": hosted[
                        "strategy_catalog_version"
                    ],
                    "max_output_tokens": hosted["max_output_tokens"],
                    "prompt_version": hosted["prompt_version"],
                    "task_schema_version": hosted["task_schema_version"],
                    "action_schema_version": hosted["action_schema_version"],
                    "capability_version": hosted["capability_version"],
                    "adapter_version": hosted["adapter_version"],
                }
                config_hash = sha256_identifier(config_snapshot)
                ready_without_payment = (
                    settlement_config.authorization_mode == "none"
                )
                await connection.execute(
                    """
                    INSERT INTO arena402.game_participants (
                        game_participant_id, game_id, user_id, agent_id,
                        runtime_binding_id, runtime_kind, portfolio_locked_at,
                        payment_mandate_id, readiness, ready_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, 'hosted', clock_timestamp(),
                        $6::text,
                        CASE WHEN (
                            NOT $9::boolean
                            AND (
                                ($6::text IS NULL AND NOT $7::boolean)
                                OR $8::boolean
                            )
                        )
                            THEN 'pending' ELSE 'ready' END,
                        CASE WHEN (
                            NOT $9::boolean
                            AND (
                                ($6::text IS NULL AND NOT $7::boolean)
                                OR $8::boolean
                            )
                        )
                            THEN NULL ELSE clock_timestamp() END
                    )
                    """,
                    participant_id,
                    game_id,
                    user_id,
                    agent_id,
                    hosted["runtime_binding_id"],
                    payment_mandate_id,
                    official_pool_join,
                    requires_game_coin_provision,
                    ready_without_payment,
                )
                await connection.execute(
                    """
                    INSERT INTO public.game_agents (
                        game_agent_id, game_id, user_id, agent_id,
                        runtime_binding_id, hosted_strategy_revision_id,
                        config_snapshot, config_hash, initial_cash_atomic,
                        initial_inventory
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9,
                        $10::jsonb
                    )
                    """,
                    participant_id,
                    game_id,
                    user_id,
                    agent_id,
                    hosted["runtime_binding_id"],
                    hosted["strategy_revision_id"],
                    _json(config_snapshot),
                    config_hash,
                    portfolio.cash_atomic,
                    _json(portfolio.holdings),
                )
                await connection.execute(
                    """
                    INSERT INTO public.hosted_agent_game_memory (
                        game_agent_id, game_id, agent_id,
                        strategy_revision_id
                    )
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (game_agent_id) DO NOTHING
                    """,
                    participant_id,
                    game_id,
                    agent_id,
                    hosted["strategy_revision_id"],
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
                    if requires_game_coin_provision:
                        assert settlement_config.chain_id is not None
                        assert settlement_config.token_address is not None
                        await connection.execute(
                            """
                            INSERT INTO arena402.game_coin_provisions (
                                provision_id, game_id, game_participant_id,
                                chain_id, token_address, account_address,
                                amount_atomic
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, $7)
                            ON CONFLICT (game_participant_id) DO NOTHING
                            """,
                            f"gcp:{participant_id}",
                            game_id,
                            participant_id,
                            settlement_config.chain_id,
                            settlement_config.token_address,
                            settlement_account.address,
                            portfolio.cash_atomic,
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
                        **(
                            {
                                "strategyArchetype": str(
                                    hosted["strategy_archetype"]
                                )
                            }
                            if official_pool_join
                            else {}
                        ),
                    },
                )
                if payment_mandate_id is not None:
                    consumed = await connection.execute(
                        """
                        UPDATE arena402.join_authorizations
                        SET status = 'consumed', consumed_at = clock_timestamp()
                        WHERE join_authorization_id = $1 AND status = 'pending'
                        """,
                        join_authorization_id,
                    )
                    if consumed != "UPDATE 1":
                        raise PawnhouseRepositoryError("mandate_not_ready")
                if (
                    not requires_game_coin_provision
                    and (payment_mandate_id is not None or official_pool_join)
                ):
                    ready_count = int(
                        await connection.fetchval(
                            """
                            SELECT count(*) FROM arena402.game_participants
                            WHERE game_id = $1 AND readiness = 'ready'
                            """,
                            game_id,
                        )
                    )
                    if ready_count >= int(phase["min_participants"]):
                        await self._start_game_locked(
                            connection,
                            game_id=game_id,
                        )
        return participant_id

    async def activate_confirmed_game_coin_provisions(
        self,
        *,
        limit: int = 100,
    ) -> dict[str, object]:
        """Promote only chain-prepared participants and start at the threshold."""

        if not 1 <= limit <= 500:
            raise ValueError("game_coin_activation_limit_invalid")
        pool = self._require_pool()
        activated: list[str] = []
        started_game_id: str | None = None
        async with pool.acquire() as connection:
            async with connection.transaction():
                rows = await connection.fetch(
                    """
                    SELECT participant.game_participant_id, participant.game_id
                    FROM arena402.game_coin_provisions AS provision
                    JOIN arena402.game_participants AS participant
                      ON participant.game_participant_id =
                         provision.game_participant_id
                    JOIN arena402.current_game AS pointer
                      ON pointer.singleton
                     AND pointer.game_id = participant.game_id
                    JOIN arena402.games AS game
                      ON game.game_id = participant.game_id
                    WHERE provision.status = 'confirmed'
                      AND participant.readiness = 'pending'
                      AND participant.status <> 'cancelled'
                      AND game.phase IN ('registration', 'portfolio_setup')
                    ORDER BY provision.confirmed_at, provision.provision_id
                    LIMIT $1
                    FOR UPDATE OF participant, game SKIP LOCKED
                    """,
                    limit,
                )
                for row in rows:
                    participant_id = str(row["game_participant_id"])
                    game_id = str(row["game_id"])
                    changed = await connection.execute(
                        """
                        UPDATE arena402.game_participants
                        SET readiness = 'ready', ready_at = clock_timestamp()
                        WHERE game_participant_id = $1
                          AND readiness = 'pending'
                        """,
                        participant_id,
                    )
                    if changed != "UPDATE 1":
                        continue
                    activated.append(participant_id)
                    await self._event(
                        connection,
                        game_id=game_id,
                        event_type="participant.settlement_ready",
                        source_key=f"{game_id}:{participant_id}:settlement-ready",
                        public_payload={"participantId": participant_id},
                    )

                game_ids = sorted({str(row["game_id"]) for row in rows})
                for game_id in game_ids:
                    game = await connection.fetchrow(
                        """
                        SELECT phase, min_participants
                        FROM arena402.games
                        WHERE game_id = $1
                        FOR UPDATE
                        """,
                        game_id,
                    )
                    if (
                        game is None
                        or game["phase"] != "portfolio_setup"
                    ):
                        continue
                    ready_count = int(
                        await connection.fetchval(
                            """
                            SELECT count(*)
                            FROM arena402.game_participants
                            WHERE game_id = $1
                              AND readiness = 'ready'
                              AND portfolio_locked_at IS NOT NULL
                            """,
                            game_id,
                        )
                    )
                    if ready_count >= int(game["min_participants"]):
                        await self._start_game_locked(
                            connection,
                            game_id=game_id,
                        )
                        started_game_id = game_id
                        break
        return {
            "activatedCount": len(activated),
            "participantIds": activated,
            "startedGameId": started_game_id,
        }

    async def add_official_hosted_participant(
        self,
        *,
        game_id: str,
        agent_id: str,
    ) -> str:
        """Join an explicitly allow-listed platform Agent, idempotently."""

        row = await self._require_pool().fetchrow(
            """
            SELECT agent.owner_user_id
            FROM arena402.official_agent_pool AS pool_entry
            JOIN public.arena_agents AS agent
              ON agent.agent_id = pool_entry.agent_id
            WHERE pool_entry.agent_id = $1
              AND pool_entry.enabled
            """,
            agent_id,
        )
        if row is None:
            raise PawnhouseRepositoryError("official_agent_not_ready")
        return await self.add_hosted_participant(
            game_id=game_id,
            user_id=str(row["owner_user_id"]),
            agent_id=agent_id,
            portfolio=None,
            require_current_game=True,
            official_pool_join=True,
        )

    async def add_current_participant(
        self,
        *,
        game_id: str,
        user_id: str,
        agent_id: str,
        portfolio: Portfolio | None,
        payment_mandate_id: str,
        join_authorization_id: str,
    ) -> str:
        """Join the current game without assuming the Agent Runtime kind."""

        runtime_kind = await self._require_pool().fetchval(
            """
            SELECT binding.runtime_kind
            FROM public.arena_agents AS agent
            JOIN public.arena_runtime_bindings AS binding
              ON binding.agent_id = agent.agent_id
             AND binding.runtime_kind IN ('hosted', 'connector')
             AND binding.disabled_at IS NULL
             AND binding.route_status = 'ready'
            WHERE agent.agent_id = $1
              AND agent.owner_user_id = $2
              AND agent.status = 'active'
            ORDER BY binding.runtime_binding_id
            LIMIT 1
            """,
            agent_id,
            user_id,
        )
        if runtime_kind == "hosted":
            return await self.add_hosted_participant(
                game_id=game_id,
                user_id=user_id,
                agent_id=agent_id,
                portfolio=portfolio,
                payment_mandate_id=payment_mandate_id,
                join_authorization_id=join_authorization_id,
                require_current_game=True,
            )
        if runtime_kind == "connector":
            return await self.add_connector_participant(
                game_id=game_id,
                user_id=user_id,
                agent_id=agent_id,
                portfolio=portfolio,
                payment_mandate_id=payment_mandate_id,
                join_authorization_id=join_authorization_id,
                require_current_game=True,
            )
        raise PawnhouseRepositoryError("runtime_not_ready")

    async def add_connector_participant(
        self,
        *,
        game_id: str,
        user_id: str,
        agent_id: str,
        portfolio: Portfolio | None,
        settlement_account: SettlementAccount | None = None,
        payment_mandate_id: str | None = None,
        join_authorization_id: str | None = None,
        require_current_game: bool = False,
    ) -> str:
        participant_id = f"gp:{game_id}:{agent_id}"
        portfolio = portfolio or default_join_portfolio(
            game_id=game_id,
            agent_id=agent_id,
        )
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                phase = await connection.fetchrow(
                    """
                    SELECT phase, max_participants, min_participants,
                           config_snapshot
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
                existing_participant = await connection.fetchrow(
                    """
                    SELECT game_participant_id, agent_id, readiness
                    FROM arena402.game_participants
                    WHERE game_id = $1 AND user_id = $2
                    FOR SHARE
                    """,
                    game_id,
                    user_id,
                )
                if existing_participant is not None:
                    if (
                        existing_participant["agent_id"] == agent_id
                        and existing_participant["readiness"] != "withdrawn"
                    ):
                        return str(
                            existing_participant["game_participant_id"]
                        )
                    raise PawnhouseRepositoryError("user_already_joined")
                participant_count = int(
                    await connection.fetchval(
                        """
                        SELECT count(*)
                        FROM arena402.game_participants
                        WHERE game_id = $1
                          AND readiness <> 'withdrawn'
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
                requires_game_coin_provision = (
                    settlement_config.authorization_mode != "none"
                    and settlement_config.token_symbol.lower()
                    == "arena402-g"
                )
                if (
                    payment_mandate_id is not None
                    and settlement_account is None
                    and settlement_config.authorization_mode != "none"
                ):
                    wallet = await connection.fetchrow(
                        """
                        SELECT chain_id, account_address
                        FROM arena402.user_wallets
                        WHERE user_id = $1
                        FOR SHARE
                        """,
                        user_id,
                    )
                    if wallet is None:
                        raise PawnhouseRepositoryError("wallet_not_ready")
                    settlement_account = SettlementAccount(
                        chain_id=int(wallet["chain_id"]),
                        address=str(wallet["account_address"]),
                        custody_mode="sandbox_guest",
                    )
                if require_current_game:
                    pointer = await connection.fetchval(
                        """
                        SELECT game_id FROM arena402.current_game
                        WHERE singleton = TRUE FOR SHARE
                        """
                    )
                    if pointer != game_id:
                        raise PawnhouseRepositoryError("game_not_current")
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
                if payment_mandate_id is not None:
                    mandate = await connection.fetchrow(
                        """
                        SELECT mandate.mandate_id
                        FROM arena402.payment_mandates AS mandate
                        JOIN arena402.join_authorizations AS join_auth
                          ON join_auth.join_authorization_id =
                             mandate.join_authorization_id
                        WHERE mandate.mandate_id = $1
                          AND mandate.user_id = $2
                          AND mandate.game_id = $3
                          AND mandate.revoked_at IS NULL
                          AND mandate.valid_from <= clock_timestamp()
                          AND mandate.expires_at > clock_timestamp()
                          AND mandate.allowed_payee_rule =
                              'same_game_settlement_account'
                          AND join_auth.join_authorization_id = $4
                          AND join_auth.user_id = $2
                          AND join_auth.game_id = $3
                          AND join_auth.agent_id = $5
                          AND join_auth.status = 'pending'
                          AND join_auth.expires_at > clock_timestamp()
                        FOR UPDATE OF mandate, join_auth
                        """,
                        payment_mandate_id,
                        user_id,
                        game_id,
                        join_authorization_id,
                        agent_id,
                    )
                    if mandate is None:
                        raise PawnhouseRepositoryError("mandate_not_ready")
                elif require_current_game:
                    raise PawnhouseRepositoryError("mandate_not_ready")
                connector = await connection.fetchrow(
                    """
                    SELECT
                        a.agent_id,
                        a.name AS display_name,
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
                    "display_name": str(
                        connector.get("display_name") or agent_id
                    ),
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
                ready_without_payment = (
                    settlement_config.authorization_mode == "none"
                )
                await connection.execute(
                    """
                    INSERT INTO arena402.game_participants (
                        game_participant_id, game_id, user_id, agent_id,
                        runtime_binding_id, runtime_kind,
                        portfolio_locked_at, payment_mandate_id,
                        readiness, ready_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, 'connector',
                        clock_timestamp(), $6::text,
                        CASE WHEN (
                            NOT $8::boolean
                            AND ($6::text IS NULL OR $7::boolean)
                        )
                            THEN 'pending' ELSE 'ready' END,
                        CASE WHEN (
                            NOT $8::boolean
                            AND ($6::text IS NULL OR $7::boolean)
                        )
                            THEN NULL ELSE clock_timestamp() END
                    )
                    """,
                    participant_id,
                    game_id,
                    user_id,
                    agent_id,
                    connector["runtime_binding_id"],
                    payment_mandate_id,
                    requires_game_coin_provision,
                    ready_without_payment,
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
                    if requires_game_coin_provision:
                        assert settlement_config.chain_id is not None
                        assert settlement_config.token_address is not None
                        await connection.execute(
                            """
                            INSERT INTO arena402.game_coin_provisions (
                                provision_id, game_id, game_participant_id,
                                chain_id, token_address, account_address,
                                amount_atomic
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, $7)
                            ON CONFLICT (game_participant_id) DO NOTHING
                            """,
                            f"gcp:{participant_id}",
                            game_id,
                            participant_id,
                            settlement_config.chain_id,
                            settlement_config.token_address,
                            settlement_account.address,
                            portfolio.cash_atomic,
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
                if payment_mandate_id is not None:
                    consumed = await connection.execute(
                        """
                        UPDATE arena402.join_authorizations
                        SET status = 'consumed',
                            consumed_at = clock_timestamp()
                        WHERE join_authorization_id = $1
                          AND status = 'pending'
                        """,
                        join_authorization_id,
                    )
                    if consumed != "UPDATE 1":
                        raise PawnhouseRepositoryError("mandate_not_ready")
                if (
                    not requires_game_coin_provision
                    and payment_mandate_id is not None
                ):
                    ready_count = int(
                        await connection.fetchval(
                            """
                            SELECT count(*) FROM arena402.game_participants
                            WHERE game_id = $1 AND readiness = 'ready'
                            """,
                            game_id,
                        )
                    )
                    if ready_count >= int(phase["min_participants"]):
                        await self._start_game_locked(
                            connection,
                            game_id=game_id,
                        )
        return participant_id

    async def withdraw_current_game_participant(
        self,
        *,
        game_id: str,
        participant_id: str,
        user_id: str,
    ) -> dict[str, object]:
        """Withdraw a waiting participant and revoke its unused mandate."""

        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                game = await connection.fetchrow(
                    """
                    SELECT game.phase
                    FROM arena402.current_game AS pointer
                    JOIN arena402.games AS game ON game.game_id = pointer.game_id
                    WHERE pointer.singleton = TRUE AND game.game_id = $1
                    FOR UPDATE OF pointer, game
                    """,
                    game_id,
                )
                if game is None:
                    raise PawnhouseRepositoryError("game_not_current")
                if game["phase"] not in {"registration", "portfolio_setup"}:
                    raise PawnhouseRepositoryError("game_already_started")
                participant = await connection.fetchrow(
                    """
                    SELECT game_participant_id, payment_mandate_id, readiness
                    FROM arena402.game_participants
                    WHERE game_participant_id = $1
                      AND game_id = $2
                      AND user_id = $3
                    FOR UPDATE
                    """,
                    participant_id,
                    game_id,
                    user_id,
                )
                if participant is None:
                    raise PawnhouseRepositoryError("participant_not_found")
                if participant["readiness"] == "withdrawn":
                    return {
                        "gameId": game_id,
                        "participantId": participant_id,
                        "withdrawn": True,
                    }
                await connection.execute(
                    """
                    UPDATE arena402.game_participants
                    SET readiness = 'withdrawn', status = 'cancelled',
                        withdrawn_at = clock_timestamp()
                    WHERE game_participant_id = $1
                    """,
                    participant_id,
                )
                if participant["payment_mandate_id"] is not None:
                    await connection.execute(
                        """
                        UPDATE arena402.payment_mandates
                        SET revoked_at = COALESCE(revoked_at, clock_timestamp())
                        WHERE mandate_id = $1
                        """,
                        participant["payment_mandate_id"],
                    )
                await self._event(
                    connection,
                    game_id=game_id,
                    event_type="participant.withdrawn",
                    source_key=f"{game_id}:{participant_id}:withdrawn",
                    public_payload={"participantId": participant_id},
                )
        return {
            "gameId": game_id,
            "participantId": participant_id,
            "withdrawn": True,
        }

    async def start_game(
        self,
        *,
        game_id: str,
        operator_user_id: str | None = None,
    ) -> dict[str, object]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                return await self._start_game_locked(
                    connection,
                    game_id=game_id,
                    operator_user_id=operator_user_id,
                )

    async def _start_game_locked(
        self,
        connection: Any,
        *,
        game_id: str,
        operator_user_id: str | None = None,
    ) -> dict[str, object]:
        """Start a game using the caller's transaction and game-row lock."""

        game = await connection.fetchrow(
            """
            SELECT phase, min_participants, round_count, operator_user_id,
                   event_seed, config_snapshot
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
            raise PawnhouseRepositoryError("game_operator_forbidden")
        if game["phase"] != "portfolio_setup":
            raise PawnhouseRepositoryError("game_not_ready")
        game_config = (
            json.loads(game["config_snapshot"])
            if isinstance(game["config_snapshot"], str)
            else dict(game["config_snapshot"])
        )
        try:
            frozen_price_catalog = price_catalog_from_snapshot(game_config)
        except ValueError:
            raise PawnhouseRepositoryError(
                "invalid_frozen_price_catalog"
            ) from None
        if _should_assign_balanced_portfolios(game_config):
            await self._assign_balanced_portfolios_locked(
                connection,
                game_id=game_id,
                seed=str(game["event_seed"]),
                base_prices=frozen_price_catalog.prices,
            )
        count = await connection.fetchval(
            """
            SELECT count(*)
            FROM arena402.game_participants
            WHERE game_id = $1
              AND readiness = 'ready'
              AND portfolio_locked_at IS NOT NULL
            """,
            game_id,
        )
        if count < game["min_participants"]:
            raise PawnhouseRepositoryError("not_enough_participants")
        events = await self._scheduled_events(connection, game_id=game_id)
        if len(events) != int(game["round_count"]):
            raise PawnhouseRepositoryError("event_schedule_incomplete")
        await connection.execute(
            """
            UPDATE arena402.games
            SET phase = 'running', current_round = 1,
                started_at = clock_timestamp()
            WHERE game_id = $1
            """,
            game_id,
        )
        await connection.execute(
            """
            UPDATE arena402.game_participants
            SET status = 'cancelled',
                completed_at = COALESCE(
                    completed_at,
                    clock_timestamp()
                )
            WHERE game_id = $1
              AND readiness <> 'ready'
              AND status IN ('joined', 'active', 'settling')
            """,
            game_id,
        )
        await connection.execute(
            """
            UPDATE public.game_agents AS game_agent
            SET status = 'cancelled',
                completed_at = COALESCE(
                    game_agent.completed_at,
                    clock_timestamp()
                )
            FROM arena402.game_participants AS participant
            WHERE participant.game_id = $1
              AND participant.game_participant_id = game_agent.game_agent_id
              AND participant.status = 'cancelled'
            """,
            game_id,
        )
        await connection.execute(
            """
            UPDATE arena402.game_participants
            SET status = 'active'
            WHERE game_id = $1
              AND readiness = 'ready'
            """,
            game_id,
        )
        round_id = f"round:{game_id}:1"
        await connection.execute(
            """
            INSERT INTO arena402.rounds (round_id, game_id, round_index, phase)
            VALUES ($1, $2, 1, 'event_reveal')
            """,
            round_id,
            game_id,
        )
        await self._event(
            connection,
            game_id=game_id,
            round_id=round_id,
            event_type="round.started",
            source_key=f"{round_id}:started",
            public_payload={
                "roundId": round_id,
                "roundIndex": 1,
            },
        )
        snapshot = WorldState(
            {event.event_id: event for event in events},
            base_prices=dict(frozen_price_catalog.prices),
        )
        world_snapshot = snapshot.reveal(events[0].event_id, round_index=1)
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
                    clock_timestamp() + (
                        SELECT action_timeout_ms FROM arena402.games
                        WHERE game_id = $2
                    ) * interval '1 millisecond'
                )
            WHERE round_id = $1
            """,
            round_id,
            game_id,
        )
        deadline_at = await connection.fetchval(
            "SELECT phase_deadline_at FROM arena402.rounds WHERE round_id = $1",
            round_id,
        )
        await connection.execute(
            """
            UPDATE public.games SET status = 'running', started_at = clock_timestamp()
            WHERE game_id = $1
            """,
            game_id,
        )
        await connection.execute(
            """
            UPDATE public.game_agents AS game_agent
            SET status = 'active'
            FROM arena402.game_participants AS participant
            WHERE participant.game_id = $1
              AND participant.game_participant_id = game_agent.game_agent_id
              AND participant.readiness = 'ready'
              AND participant.status = 'active'
            """,
            game_id,
        )
        await connection.execute(
            """
            INSERT INTO public.rounds (round_id, game_id, round_index, phase, deadline_at)
            VALUES ($1, $2, 1, 'decide', $3)
            """,
            round_id,
            game_id,
            deadline_at,
        )
        return {"gameId": game_id, "roundId": round_id, "phase": "decide"}

    async def _assign_balanced_portfolios_locked(
        self,
        connection: Any,
        *,
        game_id: str,
        seed: str,
        base_prices: Mapping[GoodId, int] = INITIAL_PRICES,
    ) -> None:
        rows = await connection.fetch(
            """
            SELECT game_participant_id
            FROM arena402.game_participants
            WHERE game_id = $1 AND readiness = 'ready'
            ORDER BY joined_at, game_participant_id
            FOR UPDATE
            """,
            game_id,
        )
        portfolios = distribute_balanced_portfolios(
            [str(row["game_participant_id"]) for row in rows],
            seed=seed,
            prices=base_prices,
        )
        for participant_id, portfolio in portfolios.items():
            await connection.execute(
                """
                UPDATE arena402.balances
                SET cash_atomic = $2, version = version + 1,
                    updated_at = clock_timestamp()
                WHERE game_participant_id = $1
                """,
                participant_id,
                portfolio.cash_atomic,
            )
            for good_id, quantity in portfolio.holdings.items():
                await connection.execute(
                    """
                    UPDATE arena402.holdings
                    SET quantity = $3, initial_quantity = $3,
                        version = version + 1, updated_at = clock_timestamp()
                    WHERE game_participant_id = $1 AND good_id = $2
                    """,
                    participant_id,
                    good_id,
                    quantity,
                )
            await connection.execute(
                """
                UPDATE public.game_agents
                SET initial_cash_atomic = $2,
                    initial_inventory = $3::jsonb
                WHERE game_agent_id = $1
                """,
                participant_id,
                portfolio.cash_atomic,
                _json(portfolio.holdings),
            )

    async def run_rule_market(self, *, game_id: str) -> dict[str, object]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                round_row = await connection.fetchrow(
                    """
                    SELECT
                        r.round_id, r.phase, r.round_index,
                        g.market_protocol
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
                if round_row["market_protocol"] != "fcfs.v1":
                    raise PawnhouseRepositoryError(
                        "rule_market_requires_fcfs_protocol"
                    )
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
                                side, good_id, quantity, limit_price_atomic
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, 1, $7)
                            RETURNING result_received_at
                            """,
                            pool_entry_id,
                            game_id,
                            round_row["round_id"],
                            participant_id,
                            source_result_id,
                            decision.action,
                            decision.good,
                            decision.target_price_atomic,
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
                    SELECT r.round_id, r.phase, g.market_protocol
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
                        "marketProtocol": str(
                            round_row["market_protocol"]
                        ),
                    },
                )
        return {
            "gameId": game_id,
            "roundId": round_row["round_id"],
            "runtimeRunId": run_id,
            "marketProtocol": str(round_row["market_protocol"]),
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
                lease_epoch = lease_epoch + 1,
                lease_expires_at = (
                    clock_timestamp()
                    + $2 * interval '1 second'
                ),
                started_at = COALESCE(started_at, clock_timestamp())
            FROM candidate
            WHERE run.runtime_run_id = candidate.runtime_run_id
            RETURNING
                run.runtime_run_id, run.game_id, run.round_id, run.stage,
                run.lease_epoch,
                (
                    SELECT game.market_protocol
                    FROM arena402.games AS game
                    WHERE game.game_id = run.game_id
                ) AS market_protocol
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
        lease_epoch: int,
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
              AND lease_epoch = $5
              AND status IN ('leased', 'running')
            RETURNING true
            """,
            runtime_run_id,
            worker_id,
            stage,
            lease_seconds,
            lease_epoch,
        )
        if not changed:
            raise PawnhouseRepositoryError("runtime_run_lease_lost")

    async def renew_hosted_run_lease(
        self,
        *,
        runtime_run_id: str,
        worker_id: str,
        lease_epoch: int,
        lease_seconds: int,
    ) -> None:
        changed = await self._require_pool().fetchval(
            """
            UPDATE arena402.runtime_runs
            SET lease_expires_at = (
                    clock_timestamp()
                    + $4 * interval '1 second'
                )
            WHERE runtime_run_id = $1
              AND leased_by = $2
              AND lease_epoch = $3
              AND status IN ('leased', 'running')
              AND lease_expires_at > clock_timestamp()
            RETURNING true
            """,
            runtime_run_id,
            worker_id,
            lease_epoch,
            lease_seconds,
        )
        if not changed:
            raise PawnhouseRepositoryError("runtime_run_lease_lost")

    async def complete_hosted_run(
        self,
        *,
        runtime_run_id: str,
        worker_id: str,
        lease_epoch: int,
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
              AND lease_epoch = $5
              AND status IN ('leased', 'running')
            RETURNING game_id, round_id
            """,
            runtime_run_id,
            worker_id,
            status,
            error_code,
            lease_epoch,
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
                    r.round_id, r.round_index, r.phase_deadline_at,
                    g.action_timeout_ms, g.round_count
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
                SELECT
                    good_id,
                    market_price_atomic,
                    final_price_atomic
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
            completed_action_rows = await connection.fetch(
                """
                SELECT
                    applied.game_agent_id,
                    applied.round_id,
                    applied.applied_action,
                    previous_round.round_index,
                    applied.authoritative_entered_at
                FROM public.arena_applied_agent_actions AS applied
                JOIN arena402.rounds AS previous_round
                  ON previous_round.round_id = applied.round_id
                 AND previous_round.game_id = applied.game_id
                WHERE applied.game_id = $1
                  AND applied.task_kind IN (
                      'arena.decide',
                      'arena.market.intent'
                  )
                  AND applied.application_outcome IN (
                      'candidate',
                      'default_pass'
                  )
                  AND applied.applied_action IS NOT NULL
                  AND previous_round.round_index < $2
                ORDER BY
                    previous_round.round_index,
                    applied.authoritative_entered_at,
                    applied.task_id
                """,
                game_id,
                int(round_row["round_index"]),
            )
            completed_trade_rows = await connection.fetch(
                """
                SELECT
                    intent.buyer_participant_id,
                    intent.seller_participant_id,
                    intent.round_id,
                    intent.negotiation_id,
                    intent.good_id,
                    intent.quantity,
                    intent.unit_price_atomic,
                    previous_round.round_index,
                    intent.completed_at
                FROM arena402.settlement_intents AS intent
                JOIN arena402.rounds AS previous_round
                  ON previous_round.round_id = intent.round_id
                 AND previous_round.game_id = intent.game_id
                WHERE intent.game_id = $1
                  AND intent.status = 'inventory_committed'
                  AND previous_round.round_index < $2
                ORDER BY
                    previous_round.round_index,
                    intent.completed_at,
                    intent.settlement_intent_id
                """,
                game_id,
                int(round_row["round_index"]),
            )
            failed_negotiation_rows = await connection.fetch(
                """
                WITH participant_failures AS (
                    SELECT buyer_participant_id AS participant_id
                    FROM arena402.pairings
                    WHERE game_id = $1
                      AND status IN ('rejected', 'timeout')
                    UNION ALL
                    SELECT seller_participant_id AS participant_id
                    FROM arena402.pairings
                    WHERE game_id = $1
                      AND status IN ('rejected', 'timeout')
                )
                SELECT participant_id, count(*) AS failed_negotiations
                FROM participant_failures
                GROUP BY participant_id
                """,
                game_id,
            )
            previous_liquidity_row = await connection.fetchrow(
                """
                SELECT
                    event.round_id,
                    previous_round.round_index,
                    event.public_payload
                FROM arena402.game_events AS event
                JOIN arena402.rounds AS previous_round
                  ON previous_round.round_id = event.round_id
                 AND previous_round.game_id = event.game_id
                WHERE event.game_id = $1
                  AND event.event_type = 'market.liquidity_summarized'
                  AND previous_round.round_index = $2 - 1
                ORDER BY
                    previous_round.round_index DESC,
                    event.created_at DESC,
                    event.event_sequence DESC
                LIMIT 1
                """,
                game_id,
                int(round_row["round_index"]),
            )
            activity_rows = await connection.fetch(
                """
                WITH decision_stats AS (
                    SELECT
                        e.public_payload->>'good' AS good_id,
                        COUNT(*) FILTER (
                            WHERE e.public_payload->>'action' = 'buy'
                        ) AS buy_count,
                        COUNT(*) FILTER (
                            WHERE e.public_payload->>'action' = 'sell'
                        ) AS sell_count
                    FROM arena402.game_events AS e
                    JOIN arena402.rounds AS previous_round
                      ON previous_round.round_id = e.round_id
                     AND previous_round.game_id = e.game_id
                    WHERE e.game_id = $1
                      AND e.event_type = 'decision.applied'
                      AND previous_round.round_index = (
                          SELECT current_round
                          FROM arena402.games
                          WHERE game_id = $1
                      ) - 1
                    GROUP BY e.public_payload->>'good'
                ),
                previous_trade_stats AS (
                    SELECT
                        i.good_id,
                        COUNT(*) AS volume
                    FROM arena402.settlement_intents AS i
                    JOIN arena402.rounds AS r
                      ON r.round_id = i.round_id
                     AND r.game_id = i.game_id
                    WHERE i.game_id = $1
                      AND i.status = 'inventory_committed'
                      AND r.round_index = (
                          SELECT current_round
                          FROM arena402.games
                          WHERE game_id = $1
                      ) - 1
                    GROUP BY i.good_id
                ),
                latest_trade AS (
                    SELECT DISTINCT ON (i.good_id)
                        i.good_id,
                        i.unit_price_atomic AS last_clearing_price_atomic
                    FROM arena402.settlement_intents AS i
                    JOIN arena402.rounds AS r
                      ON r.round_id = i.round_id
                     AND r.game_id = i.game_id
                    WHERE i.game_id = $1
                      AND i.status = 'inventory_committed'
                      AND r.round_index < (
                          SELECT current_round
                          FROM arena402.games
                          WHERE game_id = $1
                      )
                    ORDER BY
                        i.good_id,
                        r.round_index DESC,
                        i.completed_at DESC,
                        i.settlement_intent_id DESC
                )
                SELECT
                    gg.good_id,
                    COALESCE(ds.buy_count, 0) AS buy_count,
                    COALESCE(ds.sell_count, 0) AS sell_count,
                    COALESCE(pts.volume, 0) AS volume,
                    latest.last_clearing_price_atomic
                FROM arena402.game_goods AS gg
                LEFT JOIN decision_stats AS ds ON ds.good_id = gg.good_id
                LEFT JOIN previous_trade_stats AS pts
                  ON pts.good_id = gg.good_id
                LEFT JOIN latest_trade AS latest
                  ON latest.good_id = gg.good_id
                WHERE gg.game_id = $1
                ORDER BY gg.good_id
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
            actions_by_participant: dict[str, list[dict[str, object]]] = {}
            for row in completed_action_rows:
                raw_action = row["applied_action"]
                action = (
                    json.loads(raw_action)
                    if isinstance(raw_action, str)
                    else dict(raw_action)
                )
                actions_by_participant.setdefault(
                    str(row["game_agent_id"]), []
                ).append(
                    {
                        "round_id": str(row["round_id"]),
                        "round_index": int(row["round_index"]),
                        "action": action,
                    }
                )
            trades_by_participant: dict[str, list[dict[str, object]]] = {}
            for row in completed_trade_rows:
                for participant_key, role in (
                    ("buyer_participant_id", "buyer"),
                    ("seller_participant_id", "seller"),
                ):
                    trades_by_participant.setdefault(
                        str(row[participant_key]), []
                    ).append(
                        {
                            "round_id": str(row["round_id"]),
                            "round_index": int(row["round_index"]),
                            "negotiation_id": str(row["negotiation_id"]),
                            "role": role,
                            "good": str(row["good_id"]),
                            "quantity": int(row["quantity"]),
                            "price_atomic": int(row["unit_price_atomic"]),
                        }
                    )
            failures_by_participant = {
                str(row["participant_id"]): int(row["failed_negotiations"])
                for row in failed_negotiation_rows
            }
            previous_round_liquidity: dict[str, object] | None = None
            if previous_liquidity_row is not None:
                raw_payload = previous_liquidity_row["public_payload"]
                payload = (
                    json.loads(raw_payload)
                    if isinstance(raw_payload, str)
                    else dict(raw_payload)
                )
                previous_round_liquidity = {
                    **payload,
                    "roundId": str(previous_liquidity_row["round_id"]),
                    "roundIndex": int(previous_liquidity_row["round_index"]),
                }
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
                        "round_count": int(round_row["round_count"]),
                        "rounds_remaining": (
                            int(round_row["round_count"])
                            - int(round_row["round_index"])
                        ),
                        "deadline_at": round_row["phase_deadline_at"],
                        "action_timeout_ms": int(
                            round_row["action_timeout_ms"]
                        ),
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
                        "event_implied_final": {
                            row["good_id"]: int(
                                row["final_price_atomic"]
                            )
                            for row in market_rows
                        },
                        "market_activity": [
                            {
                                "good": row["good_id"],
                                "last_clearing_price_atomic": (
                                    None
                                    if row["last_clearing_price_atomic"] is None
                                    else int(row["last_clearing_price_atomic"])
                                ),
                                "volume": int(row["volume"]),
                                "buy_pressure_bps": (
                                    0
                                    if int(row["buy_count"])
                                    + int(row["sell_count"])
                                    == 0
                                    else max(
                                        -10_000,
                                        min(
                                            10_000,
                                            (
                                                (
                                                    int(row["buy_count"])
                                                    - int(row["sell_count"])
                                                )
                                                * 10_000
                                            )
                                            // (
                                                int(row["buy_count"])
                                                + int(row["sell_count"])
                                            ),
                                        ),
                                    )
                                ),
                                "spread_bps": None,
                            }
                            for row in activity_rows
                        ],
                        "previous_round_liquidity": (
                            None
                            if previous_round_liquidity is None
                            else dict(previous_round_liquidity)
                        ),
                        "completed_actions": list(
                            actions_by_participant.get(
                                str(participant["game_participant_id"]), []
                            )
                        ),
                        "completed_trades": list(
                            trades_by_participant.get(
                                str(participant["game_participant_id"]), []
                            )
                        ),
                        "failed_negotiations": failures_by_participant.get(
                            str(participant["game_participant_id"]), 0
                        ),
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

    async def agent_market_round_phase(
        self,
        *,
        game_id: str,
        round_id: str,
    ) -> str:
        """Return the persisted compatibility phase for agent_a2a.v1."""

        row = await self._require_pool().fetchrow(
            """
            SELECT round.phase
            FROM arena402.rounds AS round
            JOIN arena402.games AS game
              ON game.game_id = round.game_id
            WHERE round.game_id = $1
              AND round.round_id = $2
              AND game.market_protocol = 'agent_a2a.v1'
            """,
            game_id,
            round_id,
        )
        if row is None:
            raise PawnhouseRepositoryError(
                "agent_market_round_not_found"
            )
        return str(row["phase"])

    async def record_agent_market_liquidity_summary(
        self,
        *,
        game_id: str,
        round_id: str,
    ) -> dict[str, object]:
        """Persist one privacy-safe liquidity summary for an A2A round."""

        pool = self._require_pool()
        async with pool.acquire() as connection:
            return await self._record_agent_market_liquidity_summary(
                connection,
                game_id=game_id,
                round_id=round_id,
            )

    async def _record_agent_market_liquidity_summary(
        self,
        connection: Any,
        *,
        game_id: str,
        round_id: str,
    ) -> dict[str, object]:
        participant_count = int(
            await connection.fetchval(
                """
                SELECT count(*)
                FROM public.arena_agent_tasks
                WHERE game_id = $1
                  AND round_id = $2
                  AND task_kind = 'arena.market.intent'
                """,
                game_id,
                round_id,
            )
        )
        rows = await connection.fetch(
            """
            SELECT
                game_participant_id,
                side,
                good_id,
                limit_price_atomic
            FROM arena402.market_intents
            WHERE game_id = $1
              AND round_id = $2
            ORDER BY game_participant_id
            """,
            game_id,
            round_id,
        )
        summary = summarize_round_liquidity(
            participant_count=participant_count,
            intents=tuple(
                LiquidityIntent(
                    participant_id=str(row["game_participant_id"]),
                    side=cast(MarketSide, str(row["side"])),
                    good=require_good(str(row["good_id"])),
                    limit_price_atomic=int(row["limit_price_atomic"]),
                )
                for row in rows
            ),
        )
        payload = summary.to_public_payload()
        await self._event(
            connection,
            game_id=game_id,
            round_id=round_id,
            event_type="market.liquidity_summarized",
            source_key=f"{round_id}:market-liquidity:v1",
            public_payload=payload,
        )
        return payload

    async def advance_agent_market_stage(
        self,
        *,
        game_id: str,
        round_id: str,
        expected_phase: str,
        next_phase: str,
        market_stage: str,
    ) -> None:
        """Open the next bounded A2A stage without choosing an action."""

        allowed = {
            ("decide", "match", "rfq"),
            ("match", "negotiate", "select"),
        }
        if (expected_phase, next_phase, market_stage) not in allowed:
            raise ValueError("invalid agent market stage transition")
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT round.phase, game.action_timeout_ms
                    FROM arena402.rounds AS round
                    JOIN arena402.games AS game
                      ON game.game_id = round.game_id
                    WHERE round.game_id = $1
                      AND round.round_id = $2
                      AND game.market_protocol = 'agent_a2a.v1'
                    FOR UPDATE OF round
                    """,
                    game_id,
                    round_id,
                )
                if row is None:
                    raise PawnhouseRepositoryError(
                        "agent_market_round_not_found"
                    )
                phase = str(row["phase"])
                if phase == next_phase:
                    return
                if phase != expected_phase:
                    raise PawnhouseRepositoryError(
                        "agent_market_stage_conflict"
                    )
                deadline = await connection.fetchval(
                    """
                    UPDATE arena402.rounds
                    SET phase = $3,
                        phase_deadline_at = (
                            clock_timestamp()
                            + (
                                $4::bigint * $6::bigint
                            ) * interval '1 millisecond'
                        )
                    WHERE game_id = $1
                      AND round_id = $2
                      AND phase = $5
                    RETURNING phase_deadline_at
                    """,
                    game_id,
                    round_id,
                    next_phase,
                    int(row["action_timeout_ms"]),
                    expected_phase,
                    (
                        NEGOTIATE_STAGE_ACTION_SLOTS
                        if next_phase == "negotiate"
                        else 1
                    ),
                )
                if deadline is None:
                    raise PawnhouseRepositoryError(
                        "agent_market_stage_conflict"
                    )
                public_phase = (
                    "matching"
                    if next_phase == "match"
                    else "negotiate"
                )
                await connection.execute(
                    """
                    UPDATE public.rounds
                    SET phase = $2,
                        deadline_at = $3
                    WHERE round_id = $1
                    """,
                    round_id,
                    public_phase,
                    deadline,
                )
                await self._event(
                    connection,
                    game_id=game_id,
                    round_id=round_id,
                    event_type="market.stage_opened",
                    source_key=f"{round_id}:market-stage:{market_stage}",
                    public_payload={
                        "marketProtocol": "agent_a2a.v1",
                        "stage": market_stage,
                        "deadlineAt": deadline.isoformat(),
                    },
                )

    async def agent_market_rfq_contexts(
        self,
        *,
        game_id: str,
        round_id: str,
    ) -> list[dict[str, object]]:
        """Build unranked seller directories for every open buyer Intent."""

        pool = self._require_pool()
        async with pool.acquire() as connection:
            round_row = await connection.fetchrow(
                """
                SELECT
                    round.round_index,
                    round.phase_deadline_at,
                    game.round_count
                FROM arena402.rounds AS round
                JOIN arena402.games AS game
                  ON game.game_id = round.game_id
                WHERE round.game_id = $1
                  AND round.round_id = $2
                  AND round.phase = 'match'
                  AND game.market_protocol = 'agent_a2a.v1'
                """,
                game_id,
                round_id,
            )
            if round_row is None:
                raise PawnhouseRepositoryError(
                    "agent_market_round_not_in_rfq"
                )
            buyers = await connection.fetch(
                """
                SELECT
                    intent.*,
                    balance.cash_atomic,
                    game_agent.config_snapshot
                FROM arena402.market_intents AS intent
                JOIN arena402.balances AS balance
                  ON balance.game_participant_id =
                     intent.game_participant_id
                JOIN public.game_agents AS game_agent
                  ON game_agent.game_agent_id =
                     intent.game_participant_id
                WHERE intent.game_id = $1
                  AND intent.round_id = $2
                  AND intent.side = 'buy'
                  AND intent.status = 'open'
                  AND intent.expires_at > clock_timestamp()
                ORDER BY intent.intent_id
                """,
                game_id,
                round_id,
            )
            sellers = await connection.fetch(
                """
                SELECT
                    intent.*,
                    participant.agent_id,
                    COALESCE(agent.name, participant.agent_id)
                        AS display_name,
                    (
                        SELECT count(*)
                        FROM arena402.pairings AS prior_pairing
                        WHERE prior_pairing.game_id = intent.game_id
                          AND prior_pairing.status IN ('rejected', 'timeout')
                          AND (
                              prior_pairing.buyer_participant_id =
                                  intent.game_participant_id
                              OR prior_pairing.seller_participant_id =
                                  intent.game_participant_id
                          )
                    ) AS failed_negotiations
                FROM arena402.market_intents AS intent
                JOIN arena402.game_participants AS participant
                  ON participant.game_participant_id =
                     intent.game_participant_id
                 AND participant.game_id = intent.game_id
                LEFT JOIN public.arena_agents AS agent
                  ON agent.agent_id = participant.agent_id
                WHERE intent.game_id = $1
                  AND intent.round_id = $2
                  AND intent.side = 'sell'
                  AND intent.status = 'open'
                  AND intent.expires_at > clock_timestamp()
                ORDER BY intent.intent_id
                """,
                game_id,
                round_id,
            )
            event_rows = await connection.fetch(
                """
                SELECT event_id, public_snapshot, revealed_at
                FROM arena402.event_occurrences
                WHERE game_id = $1
                  AND round_index <= $2
                ORDER BY round_index, event_id
                """,
                game_id,
                int(round_row["round_index"]),
            )
        contexts: list[dict[str, object]] = []
        for buyer in buyers:
            directory = []
            for seller in sellers:
                if (
                    str(seller["good_id"]) != str(buyer["good_id"])
                    or str(seller["game_participant_id"])
                    == str(buyer["game_participant_id"])
                    or int(buyer["limit_price_atomic"])
                    < int(seller["limit_price_atomic"])
                ):
                    continue
                directory.append(
                    {
                        "intent_id": str(seller["intent_id"]),
                        "agent_id": str(seller["agent_id"]),
                        "display_name": str(seller["display_name"]),
                        "good": str(seller["good_id"]),
                        "quantity": int(seller["quantity"]),
                        "public_price_atomic": int(
                            seller["public_price_atomic"]
                        ),
                        "failed_negotiations": int(
                            seller["failed_negotiations"]
                        ),
                        "expires_at": seller["expires_at"],
                    }
                )
            session = await pool.fetchrow(
                """
                INSERT INTO arena402.market_rfq_sessions (
                    buyer_intent_id,
                    game_id,
                    round_id,
                    buyer_participant_id,
                    frozen_directory,
                    deadline_at
                )
                VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                ON CONFLICT (buyer_intent_id) DO UPDATE
                SET buyer_intent_id = EXCLUDED.buyer_intent_id
                RETURNING
                    frozen_directory,
                    attempt_count,
                    max_attempts,
                    status,
                    deadline_at
                """,
                buyer["intent_id"],
                game_id,
                round_id,
                buyer["game_participant_id"],
                json.dumps(
                    directory,
                    default=lambda value: value.isoformat(),
                ),
                buyer["expires_at"],
            )
            if (
                session is None
                or str(session["status"]) != "active"
                or int(session["attempt_count"])
                >= int(session["max_attempts"])
            ):
                continue
            prior_rows = await pool.fetch(
                """
                SELECT
                    attempt_sequence,
                    seller_intent_id,
                    status
                FROM arena402.market_negotiation_requests
                WHERE buyer_intent_id = $1
                ORDER BY attempt_sequence
                """,
                buyer["intent_id"],
            )
            if any(
                str(prior["status"]) in {"pending", "engaged"}
                for prior in prior_rows
            ):
                continue
            frozen = session["frozen_directory"]
            if isinstance(frozen, str):
                frozen = json.loads(frozen)
            attempted_targets = {
                str(prior["seller_intent_id"]) for prior in prior_rows
            }
            directory = [
                dict(entry)
                for entry in list(frozen)
                if str(entry["intent_id"]) not in attempted_targets
            ]
            if not directory:
                continue
            config = buyer["config_snapshot"]
            contexts.append(
                {
                    "game_id": game_id,
                    "round_id": round_id,
                    "round_index": int(round_row["round_index"]),
                    "round_count": int(round_row["round_count"]),
                    "rounds_remaining": (
                        int(round_row["round_count"])
                        - int(round_row["round_index"])
                    ),
                    "deadline_at": round_row["phase_deadline_at"],
                    "participant_id": str(
                        buyer["game_participant_id"]
                    ),
                    "buyer_intent_id": str(buyer["intent_id"]),
                    "good": str(buyer["good_id"]),
                    "quantity": int(buyer["quantity"]),
                    "public_price_atomic": int(
                        buyer["public_price_atomic"]
                    ),
                    "limit_price_atomic": int(
                        buyer["limit_price_atomic"]
                    ),
                    "cash_atomic": int(buyer["cash_atomic"]),
                    "directory": directory,
                    "attempt_sequence": (
                        int(session["attempt_count"]) + 1
                    ),
                    "remaining_rfq_attempts": (
                        int(session["max_attempts"])
                        - int(session["attempt_count"])
                    ),
                    "prior_attempts": [
                        {
                            "attempt_sequence": int(
                                prior["attempt_sequence"]
                            ),
                            "target_intent_id": str(
                                prior["seller_intent_id"]
                            ),
                            "status": (
                                "timed_out"
                                if str(prior["status"]) == "expired"
                                else str(prior["status"])
                            ),
                        }
                        for prior in prior_rows
                    ],
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
                        json.loads(config)
                        if isinstance(config, str)
                        else dict(config)
                    ),
                }
            )
        return contexts

    async def agent_market_fallback_rfq_contexts(
        self,
        *,
        game_id: str,
        round_id: str,
    ) -> list[dict[str, object]]:
        """Resume only buyer-chosen RFQs from the original frozen directory."""

        pool = self._require_pool()
        round_row = await pool.fetchrow(
            """
            SELECT
                round.round_index,
                round.phase_deadline_at,
                game.action_timeout_ms,
                game.round_count
            FROM arena402.rounds AS round
            JOIN arena402.games AS game
              ON game.game_id = round.game_id
            WHERE round.game_id = $1
              AND round.round_id = $2
              AND round.phase = 'negotiate'
              AND round.phase_deadline_at > clock_timestamp()
              AND game.market_protocol = 'agent_a2a.v1'
            """,
            game_id,
            round_id,
        )
        if round_row is None:
            return []
        buyers = await pool.fetch(
            """
            SELECT
                session.*,
                intent.good_id,
                intent.quantity,
                intent.public_price_atomic,
                intent.limit_price_atomic,
                balance.cash_atomic,
                game_agent.config_snapshot
            FROM arena402.market_rfq_sessions AS session
            JOIN arena402.market_intents AS intent
              ON intent.intent_id = session.buyer_intent_id
            JOIN arena402.balances AS balance
              ON balance.game_participant_id =
                 session.buyer_participant_id
            JOIN public.game_agents AS game_agent
              ON game_agent.game_agent_id =
                 session.buyer_participant_id
            WHERE session.game_id = $1
              AND session.round_id = $2
              AND session.status = 'active'
              AND session.attempt_count BETWEEN 1
                                            AND session.max_attempts - 1
              AND session.deadline_at > clock_timestamp()
              AND intent.status = 'open'
              AND intent.expires_at > clock_timestamp()
              AND NOT EXISTS (
                  SELECT 1
                  FROM arena402.market_negotiation_requests AS request
                  WHERE request.buyer_intent_id =
                        session.buyer_intent_id
                    AND request.status IN ('pending', 'engaged')
              )
            ORDER BY session.buyer_intent_id
            """,
            game_id,
            round_id,
        )
        live_seller_ids = {
            str(row["intent_id"])
            for row in await pool.fetch(
                """
                SELECT intent_id
                FROM arena402.market_intents
                WHERE game_id = $1
                  AND round_id = $2
                  AND side = 'sell'
                  AND status = 'open'
                  AND expires_at > clock_timestamp()
                """,
                game_id,
                round_id,
            )
        }
        event_rows = await pool.fetch(
            """
            SELECT event_id, public_snapshot, revealed_at
            FROM arena402.event_occurrences
            WHERE game_id = $1
              AND round_index <= $2
            ORDER BY round_index, event_id
            """,
            game_id,
            int(round_row["round_index"]),
        )
        contexts: list[dict[str, object]] = []
        for buyer in buyers:
            prior_rows = await pool.fetch(
                """
                SELECT
                    attempt_sequence,
                    seller_intent_id,
                    status
                FROM arena402.market_negotiation_requests
                WHERE buyer_intent_id = $1
                ORDER BY attempt_sequence
                """,
                buyer["buyer_intent_id"],
            )
            if len(prior_rows) != int(buyer["attempt_count"]):
                raise PawnhouseRepositoryError(
                    "agent_market_rfq_attempt_state_invalid"
                )
            attempted_targets = {
                str(prior["seller_intent_id"]) for prior in prior_rows
            }
            frozen = buyer["frozen_directory"]
            if isinstance(frozen, str):
                frozen = json.loads(frozen)
            directory = [
                dict(entry)
                for entry in list(frozen)
                if str(entry["intent_id"]) not in attempted_targets
                and str(entry["intent_id"]) in live_seller_ids
            ]
            if not directory:
                await pool.execute(
                    """
                    UPDATE arena402.market_rfq_sessions
                    SET status = 'completed',
                        updated_at = clock_timestamp()
                    WHERE buyer_intent_id = $1
                      AND status = 'active'
                    """,
                    buyer["buyer_intent_id"],
                )
                continue
            config = buyer["config_snapshot"]
            contexts.append(
                {
                    "game_id": game_id,
                    "round_id": round_id,
                    "round_index": int(round_row["round_index"]),
                    "round_count": int(round_row["round_count"]),
                    "rounds_remaining": (
                        int(round_row["round_count"])
                        - int(round_row["round_index"])
                    ),
                    "deadline_at": min(
                        round_row["phase_deadline_at"],
                        buyer["deadline_at"],
                        datetime.now(timezone.utc)
                        + timedelta(
                            milliseconds=int(
                                round_row["action_timeout_ms"]
                            )
                        ),
                    ),
                    "participant_id": str(
                        buyer["buyer_participant_id"]
                    ),
                    "buyer_intent_id": str(buyer["buyer_intent_id"]),
                    "good": str(buyer["good_id"]),
                    "quantity": int(buyer["quantity"]),
                    "public_price_atomic": int(
                        buyer["public_price_atomic"]
                    ),
                    "limit_price_atomic": int(
                        buyer["limit_price_atomic"]
                    ),
                    "cash_atomic": int(buyer["cash_atomic"]),
                    "directory": directory,
                    "attempt_sequence": int(buyer["attempt_count"]) + 1,
                    "remaining_rfq_attempts": (
                        int(buyer["max_attempts"])
                        - int(buyer["attempt_count"])
                    ),
                    "prior_attempts": [
                        {
                            "attempt_sequence": int(
                                prior["attempt_sequence"]
                            ),
                            "target_intent_id": str(
                                prior["seller_intent_id"]
                            ),
                            "status": (
                                "timed_out"
                                if str(prior["status"]) == "expired"
                                else str(prior["status"])
                            ),
                        }
                        for prior in prior_rows
                    ],
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
                        json.loads(config)
                        if isinstance(config, str)
                        else dict(config)
                    ),
                }
            )
        return contexts

    async def agent_market_select_contexts(
        self,
        *,
        game_id: str,
        round_id: str,
    ) -> list[dict[str, object]]:
        """Build each seller's frozen inbound RFQ set without ranking it."""

        pool = self._require_pool()
        async with pool.acquire() as connection:
            round_row = await connection.fetchrow(
                """
                SELECT
                    round.round_index,
                    round.phase_deadline_at,
                    game.action_timeout_ms,
                    game.round_count
                FROM arena402.rounds AS round
                JOIN arena402.games AS game
                  ON game.game_id = round.game_id
                WHERE round.game_id = $1
                  AND round.round_id = $2
                  AND round.phase = 'negotiate'
                  AND game.market_protocol = 'agent_a2a.v1'
                """,
                game_id,
                round_id,
            )
            if round_row is None:
                raise PawnhouseRepositoryError(
                    "agent_market_round_not_in_select"
                )
            sellers = await connection.fetch(
                """
                SELECT
                    intent.*,
                    holding.quantity AS inventory_available,
                    game_agent.config_snapshot
                FROM arena402.market_intents AS intent
                JOIN arena402.holdings AS holding
                  ON holding.game_participant_id =
                     intent.game_participant_id
                 AND holding.good_id = intent.good_id
                JOIN public.game_agents AS game_agent
                  ON game_agent.game_agent_id =
                     intent.game_participant_id
                WHERE intent.game_id = $1
                  AND intent.round_id = $2
                  AND intent.side = 'sell'
                  AND intent.status = 'open'
                  AND intent.expires_at > clock_timestamp()
                  AND EXISTS (
                      SELECT 1
                      FROM arena402.market_negotiation_requests AS request
                      WHERE request.seller_intent_id = intent.intent_id
                        AND request.status = 'pending'
                  )
                ORDER BY intent.intent_id
                """,
                game_id,
                round_id,
            )
            event_rows = await connection.fetch(
                """
                SELECT event_id, public_snapshot, revealed_at
                FROM arena402.event_occurrences
                WHERE game_id = $1
                  AND round_index <= $2
                ORDER BY round_index, event_id
                """,
                game_id,
                int(round_row["round_index"]),
            )
            contexts: list[dict[str, object]] = []
            for seller in sellers:
                requests = await connection.fetch(
                    """
                    SELECT
                        request.request_id,
                        buyer_participant.agent_id AS buyer_agent_id,
                        COALESCE(
                            buyer_agent.name,
                            buyer_participant.agent_id
                        ) AS buyer_display_name,
                        request.opening_price_atomic,
                        request.public_message,
                        request.created_at
                    FROM arena402.market_negotiation_requests AS request
                    JOIN arena402.game_participants AS buyer_participant
                      ON buyer_participant.game_participant_id =
                         request.buyer_participant_id
                     AND buyer_participant.game_id = request.game_id
                    LEFT JOIN public.arena_agents AS buyer_agent
                      ON buyer_agent.agent_id =
                         buyer_participant.agent_id
                    WHERE request.seller_intent_id = $1
                      AND request.status = 'pending'
                    ORDER BY request.created_at, request.request_id
                    """,
                    seller["intent_id"],
                )
                config = seller["config_snapshot"]
                contexts.append(
                    {
                        "game_id": game_id,
                        "round_id": round_id,
                        "round_index": int(round_row["round_index"]),
                        "round_count": int(round_row["round_count"]),
                        "rounds_remaining": (
                            int(round_row["round_count"])
                            - int(round_row["round_index"])
                        ),
                        "deadline_at": min(
                            round_row["phase_deadline_at"],
                            datetime.now(timezone.utc)
                            + timedelta(
                                milliseconds=int(
                                    round_row["action_timeout_ms"]
                                )
                            ),
                        ),
                        "participant_id": str(
                            seller["game_participant_id"]
                        ),
                        "seller_intent_id": str(seller["intent_id"]),
                        "good": str(seller["good_id"]),
                        "quantity": int(seller["quantity"]),
                        "public_price_atomic": int(
                            seller["public_price_atomic"]
                        ),
                        "limit_price_atomic": int(
                            seller["limit_price_atomic"]
                        ),
                        "inventory_available": int(
                            seller["inventory_available"]
                        ),
                        "requests": [
                            {
                                "request_id": str(row["request_id"]),
                                "buyer_agent_id": str(
                                    row["buyer_agent_id"]
                                ),
                                "buyer_display_name": str(
                                    row["buyer_display_name"]
                                ),
                                "opening_price_atomic": int(
                                    row["opening_price_atomic"]
                                ),
                                "message": str(row["public_message"]),
                                "received_at": row["created_at"],
                            }
                            for row in requests
                        ],
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
                            json.loads(config)
                            if isinstance(config, str)
                            else dict(config)
                        ),
                    }
                )
        return contexts

    async def project_agent_market_result(
        self,
        *,
        result_id: str,
    ) -> dict[str, object]:
        """Synchronously reach the durable projection boundary for one Result."""

        row = await self._require_pool().fetchrow(
            """
            SELECT
                applied.task_id,
                applied.result_id,
                applied.task_kind,
                applied.application_outcome,
                applied.applied_action,
                applied.authoritative_entered_at,
                applied.applied_at,
                receipt.result_id AS projected_result_id
            FROM public.arena_applied_agent_actions AS applied
            LEFT JOIN arena402.market_projection_receipts AS receipt
              ON receipt.result_id = applied.result_id
            WHERE applied.result_id = $1
              AND applied.task_kind IN (
                  'arena.market.intent',
                  'arena.market.rfq',
                  'arena.market.select'
              )
            """,
            result_id,
        )
        if row is None:
            raise PawnhouseRepositoryError(
                "agent_market_application_not_found"
            )
        if row["projected_result_id"] is not None:
            return {
                "taskId": str(row["task_id"]),
                "resultId": str(row["result_id"]),
                "kind": str(row["task_kind"]),
                "outcome": str(row["application_outcome"]),
                "projected": True,
                "replayed": True,
            }
        raw_action = row["applied_action"]
        if isinstance(raw_action, str):
            raw_action = json.loads(raw_action)
        application = AppliedArenaAction(
            task_id=str(row["task_id"]),
            result_id=str(row["result_id"]),
            kind=str(row["task_kind"]),  # type: ignore[arg-type]
            outcome=str(row["application_outcome"]),
            action=(
                None
                if raw_action is None
                else dict(raw_action)
            ),
            entered_at=row["authoritative_entered_at"],
            applied_at=row["applied_at"],
        )
        return await self.project_agent_market_application(application)

    async def materialize_agent_market_engagements(
        self,
        *,
        game_id: str,
        round_id: str,
    ) -> list[str]:
        """Create negotiation records only from Agent-selected Engagements."""

        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                rows = await connection.fetch(
                    """
                    SELECT
                        engagement.*,
                        buyer.limit_price_atomic
                            AS buyer_limit_price_atomic,
                        seller.limit_price_atomic
                            AS seller_limit_price_atomic,
                        request.opening_price_atomic,
                        request.source_result_id
                            AS rfq_result_id,
                        request.public_message
                            AS rfq_public_message
                    FROM arena402.market_engagements AS engagement
                    JOIN arena402.market_intents AS buyer
                      ON buyer.intent_id = engagement.buyer_intent_id
                    JOIN arena402.market_intents AS seller
                      ON seller.intent_id = engagement.seller_intent_id
                    JOIN arena402.market_negotiation_requests AS request
                      ON request.request_id = engagement.request_id
                    LEFT JOIN arena402.negotiations AS negotiation
                      ON negotiation.negotiation_id =
                         engagement.negotiation_id
                    WHERE engagement.game_id = $1
                      AND engagement.round_id = $2
                      AND engagement.status = 'active'
                      AND negotiation.negotiation_id IS NULL
                    ORDER BY engagement.engagement_id
                    FOR UPDATE OF engagement
                    """,
                    game_id,
                    round_id,
                )
                negotiation_ids: list[str] = []
                for row in rows:
                    buyer_entry_id = (
                        f"pool:market:{row['engagement_id']}:buyer"
                    )
                    seller_entry_id = (
                        f"pool:market:{row['engagement_id']}:seller"
                    )
                    for (
                        entry_id,
                        participant_id,
                        source_result_id,
                        side,
                        limit_price,
                    ) in (
                        (
                            buyer_entry_id,
                            row["buyer_participant_id"],
                            row["rfq_result_id"],
                            "buy",
                            row["buyer_limit_price_atomic"],
                        ),
                        (
                            seller_entry_id,
                            row["seller_participant_id"],
                            row["selection_result_id"],
                            "sell",
                            row["seller_limit_price_atomic"],
                        ),
                    ):
                        await connection.execute(
                            """
                            INSERT INTO arena402.pool_entries (
                                pool_entry_id,
                                game_id,
                                round_id,
                                game_participant_id,
                                source_result_id,
                                side,
                                good_id,
                                quantity,
                                limit_price_atomic,
                                market_engagement_id,
                                status
                            )
                            VALUES (
                                $1, $2, $3, $4, $5, $6, $7, 1, $8,
                                $9, 'paired'
                            )
                            ON CONFLICT (source_result_id) DO NOTHING
                            """,
                            entry_id,
                            game_id,
                            round_id,
                            participant_id,
                            source_result_id,
                            side,
                            row["good_id"],
                            limit_price,
                            row["engagement_id"],
                        )
                    pairing_id = f"pairing:{row['engagement_id']}"
                    sequence = int(
                        await connection.fetchval(
                            """
                            SELECT COALESCE(max(pairing_sequence), 0) + 1
                            FROM arena402.pairings
                            WHERE round_id = $1
                              AND good_id = $2
                            """,
                            round_id,
                            row["good_id"],
                        )
                    )
                    await connection.execute(
                        """
                        INSERT INTO arena402.pairings (
                            pairing_id,
                            game_id,
                            round_id,
                            good_id,
                            buyer_entry_id,
                            seller_entry_id,
                            buyer_participant_id,
                            seller_participant_id,
                            pairing_sequence,
                            quantity,
                            buyer_limit_price_atomic,
                            seller_limit_price_atomic
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, 1,
                            $10, $11
                        )
                        ON CONFLICT (pairing_id) DO NOTHING
                        """,
                        pairing_id,
                        game_id,
                        round_id,
                        row["good_id"],
                        buyer_entry_id,
                        seller_entry_id,
                        row["buyer_participant_id"],
                        row["seller_participant_id"],
                        sequence,
                        row["buyer_limit_price_atomic"],
                        row["seller_limit_price_atomic"],
                    )
                    await connection.execute(
                        """
                        INSERT INTO arena402.negotiations (
                            negotiation_id,
                            pairing_id,
                            game_id,
                            round_id,
                            buyer_participant_id,
                            seller_participant_id,
                            max_turns,
                            turn_count,
                            next_role,
                            latest_proposal_price_atomic,
                            latest_proposal_role,
                            action_deadline_at
                        )
                        SELECT
                            $1, $2, $3, $4, $5, $6,
                            game.max_negotiation_turns,
                            1,
                            'seller',
                            $7,
                            'buyer',
                            clock_timestamp()
                            + game.action_timeout_ms
                              * interval '1 millisecond'
                        FROM arena402.games AS game
                        WHERE game.game_id = $3
                          AND game.market_protocol = 'agent_a2a.v1'
                        ON CONFLICT (negotiation_id) DO NOTHING
                        """,
                        row["negotiation_id"],
                        pairing_id,
                        game_id,
                        round_id,
                        row["buyer_participant_id"],
                        row["seller_participant_id"],
                        row["opening_price_atomic"],
                    )
                    await connection.execute(
                        """
                        INSERT INTO arena402.negotiation_messages (
                            negotiation_message_id,
                            negotiation_id,
                            game_id,
                            round_id,
                            source_result_id,
                            turn_sequence,
                            actor_role,
                            action,
                            price_atomic,
                            public_message,
                            result_received_at
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, 1, 'buyer',
                            'propose', $6, $7, $8
                        )
                        ON CONFLICT (source_result_id) DO NOTHING
                        """,
                        f"msg:{row['negotiation_id']}:1",
                        row["negotiation_id"],
                        game_id,
                        round_id,
                        row["rfq_result_id"],
                        row["opening_price_atomic"],
                        row["rfq_public_message"],
                        row["created_at"],
                    )
                    await self._event(
                        connection,
                        game_id=game_id,
                        round_id=round_id,
                        event_type="negotiation.message",
                        source_key=(
                            f"{row['rfq_result_id']}:"
                            f"{row['negotiation_id']}:binding-opening"
                        ),
                        public_payload={
                            "negotiationId": str(
                                row["negotiation_id"]
                            ),
                            "turn": 1,
                            "role": "buyer",
                            "action": "propose",
                            "priceAtomic": str(
                                row["opening_price_atomic"]
                            ),
                            "message": str(
                                row["rfq_public_message"]
                            ),
                        },
                    )
                    await self._event(
                        connection,
                        game_id=game_id,
                        round_id=round_id,
                        event_type="market.negotiation_created",
                        source_key=(
                            f"{row['engagement_id']}:"
                            "negotiation-created"
                        ),
                        public_payload={
                            "engagementId": str(row["engagement_id"]),
                            "negotiationId": str(row["negotiation_id"]),
                            "requestId": str(row["request_id"]),
                            "good": str(row["good_id"]),
                            "bindingOpeningPriceAtomic": str(
                                row["opening_price_atomic"]
                            ),
                            "bindingOpeningResultId": str(
                                row["rfq_result_id"]
                            ),
                        },
                    )
                    negotiation_ids.append(str(row["negotiation_id"]))
        return negotiation_ids

    async def pending_agent_market_applications(
        self,
        *,
        limit: int = 100,
    ) -> list[AppliedArenaAction]:
        """Return applied market Results not yet durably projected."""

        if limit <= 0:
            return []
        rows = await self._require_pool().fetch(
            """
            SELECT
                applied.task_id,
                applied.result_id,
                applied.task_kind,
                applied.application_outcome,
                applied.applied_action,
                applied.authoritative_entered_at,
                applied.applied_at
            FROM public.arena_applied_agent_actions AS applied
            LEFT JOIN arena402.market_projection_receipts AS receipt
              ON receipt.result_id = applied.result_id
            WHERE applied.task_kind IN (
                'arena.market.intent',
                'arena.market.rfq',
                'arena.market.select'
            )
              AND receipt.result_id IS NULL
            ORDER BY applied.applied_at, applied.task_id
            LIMIT $1
            """,
            min(limit, 1000),
        )
        applications: list[AppliedArenaAction] = []
        for row in rows:
            action = row["applied_action"]
            if isinstance(action, str):
                action = json.loads(action)
            applications.append(
                AppliedArenaAction(
                    task_id=str(row["task_id"]),
                    result_id=str(row["result_id"]),
                    kind=str(row["task_kind"]),  # type: ignore[arg-type]
                    outcome=str(row["application_outcome"]),
                    action=(
                        None
                        if action is None
                        else dict(action)
                    ),
                    entered_at=row["authoritative_entered_at"],
                    applied_at=row["applied_at"],
                )
            )
        return applications

    async def project_agent_market_application(
        self,
        application: AppliedArenaAction,
    ) -> dict[str, object]:
        """Project one applied real-Agent market Result idempotently.

        The caller supplies only an applied-action pointer. This method
        re-reads the authoritative task, input, action, and Result provenance
        inside the database transaction before touching market state.
        """

        if application.kind not in {
            "arena.market.intent",
            "arena.market.rfq",
            "arena.market.select",
        }:
            raise PawnhouseRepositoryError(
                "agent_market_task_kind_invalid"
            )
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                # The coordinator and the projection worker can observe the
                # same newly-applied Result. Serialize that Result before
                # reading its receipt so the loser sees the winner's committed
                # projection instead of re-running mutable RFQ checks.
                await connection.execute(
                    """
                    SELECT pg_advisory_xact_lock(
                        hashtextextended($1::text, 0)
                    )
                    """,
                    application.result_id,
                )
                existing_receipt = await connection.fetchrow(
                    """
                    SELECT
                        receipt.task_id,
                        receipt.result_id,
                        receipt.task_kind,
                        receipt.application_outcome
                    FROM arena402.market_projection_receipts AS receipt
                    WHERE receipt.result_id = $1
                    """,
                    application.result_id,
                )
                if existing_receipt is not None:
                    if (
                        str(existing_receipt["task_id"])
                        != application.task_id
                        or str(existing_receipt["task_kind"])
                        != application.kind
                        or str(existing_receipt["application_outcome"])
                        != application.outcome
                    ):
                        raise PawnhouseRepositoryError(
                            "agent_market_projection_receipt_conflict"
                        )
                    return {
                        "taskId": application.task_id,
                        "resultId": application.result_id,
                        "kind": application.kind,
                        "outcome": application.outcome,
                        "projected": True,
                        "replayed": True,
                    }
                row = await connection.fetchrow(
                    """
                    SELECT
                        task.task_id,
                        task.task_kind,
                        task.game_id,
                        task.round_id,
                        task.game_agent_id,
                        task.deadline_at,
                        task.input_snapshot,
                        round.phase AS current_round_phase,
                        applied.result_id,
                        applied.application_outcome,
                        applied.applied_action,
                        applied.authoritative_entered_at
                    FROM public.arena_applied_agent_actions AS applied
                    JOIN public.arena_agent_tasks AS task
                      ON task.task_id = applied.task_id
                    JOIN arena402.rounds AS round
                      ON round.round_id = task.round_id
                     AND round.game_id = task.game_id
                    WHERE applied.task_id = $1
                      AND applied.result_id = $2
                      AND applied.task_kind = $3
                    """,
                    application.task_id,
                    application.result_id,
                    application.kind,
                )
                if row is None:
                    raise PawnhouseRepositoryError(
                        "agent_market_application_not_found"
                    )
                outcome = str(row["application_outcome"])
                if outcome != "candidate":
                    input_snapshot = row["input_snapshot"]
                    if isinstance(input_snapshot, str):
                        input_snapshot = json.loads(input_snapshot)
                    if (
                        outcome == "market_timeout"
                        and isinstance(input_snapshot, Mapping)
                    ):
                        if application.kind == "arena.market.rfq":
                            task_input = (
                                ArenaMarketRfqInputV1.model_validate(
                                    input_snapshot
                                )
                            )
                            await connection.execute(
                                """
                                UPDATE arena402.market_rfq_sessions
                                SET status = 'expired',
                                    updated_at = clock_timestamp()
                                WHERE buyer_intent_id = $1
                                  AND status = 'active'
                                """,
                                task_input.buyer_intent_id,
                            )
                        elif application.kind == "arena.market.select":
                            task_input = (
                                ArenaMarketSelectInputV1.model_validate(
                                    input_snapshot
                                )
                            )
                            await connection.execute(
                                """
                                UPDATE arena402.market_negotiation_requests
                                SET status = 'expired'
                                WHERE request_id = ANY($1::text[])
                                  AND status = 'pending'
                                """,
                                [
                                    request.request_id
                                    for request in task_input.requests
                                ],
                            )
                    projection = {
                        "taskId": str(row["task_id"]),
                        "resultId": str(row["result_id"]),
                        "kind": str(row["task_kind"]),
                        "outcome": outcome,
                        "projected": False,
                    }
                    await self._record_market_projection_locked(
                        connection,
                        row=row,
                    )
                    return projection

                allowed_round_phases = {
                    "arena.market.intent": {"decide"},
                    "arena.market.rfq": {"match", "negotiate"},
                    "arena.market.select": {"negotiate"},
                }[application.kind]
                if str(row["current_round_phase"]) not in allowed_round_phases:
                    projection = {
                        "taskId": str(row["task_id"]),
                        "resultId": str(row["result_id"]),
                        "kind": str(row["task_kind"]),
                        "outcome": outcome,
                        "projected": False,
                        "rejectionReason": "market_stage_closed",
                    }
                    await self._record_market_projection_locked(
                        connection,
                        row=row,
                    )
                    return projection

                input_snapshot = row["input_snapshot"]
                if isinstance(input_snapshot, str):
                    input_snapshot = json.loads(input_snapshot)
                applied_action = row["applied_action"]
                if isinstance(applied_action, str):
                    applied_action = json.loads(applied_action)
                if not isinstance(input_snapshot, Mapping) or not isinstance(
                    applied_action,
                    Mapping,
                ):
                    raise PawnhouseRepositoryError(
                        "agent_market_application_invalid"
                    )

                try:
                    if application.kind == "arena.market.intent":
                        task_input = ArenaMarketIntentInputV1.model_validate(
                            input_snapshot
                        )
                        action = _MARKET_INTENT_ACTION_ADAPTER.validate_python(
                            dict(applied_action),
                            strict=True,
                        )
                        violation = market_intent_candidate_violation(
                            task_input,
                            action,
                        )
                        if violation is not None:
                            projection = {
                                "taskId": str(row["task_id"]),
                                "resultId": str(row["result_id"]),
                                "kind": str(row["task_kind"]),
                                "outcome": "default_pass",
                                "projected": False,
                                "rejectionReason": violation,
                            }
                            await self._record_market_projection_locked(
                                connection,
                                row=row,
                            )
                            return projection
                        projection = await self._project_market_intent_locked(
                            connection,
                            row=row,
                            task_input=task_input,
                            action=action,
                        )
                    elif application.kind == "arena.market.rfq":
                        task_input = ArenaMarketRfqInputV1.model_validate(
                            input_snapshot
                        )
                        action = _MARKET_RFQ_ACTION_ADAPTER.validate_python(
                            dict(applied_action),
                            strict=True,
                        )
                        violation = market_rfq_candidate_violation(
                            task_input,
                            action,
                        )
                        if violation is not None:
                            projection = {
                                "taskId": str(row["task_id"]),
                                "resultId": str(row["result_id"]),
                                "kind": str(row["task_kind"]),
                                "outcome": "market_timeout",
                                "projected": False,
                                "rejectionReason": violation,
                            }
                            await self._record_market_projection_locked(
                                connection,
                                row=row,
                            )
                            return projection
                        projection = await self._project_market_rfq_locked(
                            connection,
                            row=row,
                            task_input=task_input,
                            action=action,
                        )
                    else:
                        task_input = ArenaMarketSelectInputV1.model_validate(
                            input_snapshot
                        )
                        action = (
                            _MARKET_SELECT_ACTION_ADAPTER.validate_python(
                                dict(applied_action),
                                strict=True,
                            )
                        )
                        violation = market_select_candidate_violation(
                            task_input,
                            action,
                        )
                        if violation is not None:
                            projection = {
                                "taskId": str(row["task_id"]),
                                "resultId": str(row["result_id"]),
                                "kind": str(row["task_kind"]),
                                "outcome": "market_timeout",
                                "projected": False,
                                "rejectionReason": violation,
                            }
                            await self._record_market_projection_locked(
                                connection,
                                row=row,
                            )
                            return projection
                        projection = (
                            await self._project_market_selection_locked(
                                connection,
                                row=row,
                                task_input=task_input,
                                action=action,
                            )
                        )
                    await self._record_market_projection_locked(
                        connection,
                        row=row,
                    )
                    return projection
                except ValidationError:
                    raise PawnhouseRepositoryError(
                        "agent_market_application_invalid"
                    ) from None

    @staticmethod
    async def _record_market_projection_locked(
        connection: Any,
        *,
        row: Mapping[str, object],
    ) -> None:
        receipt = await connection.fetchrow(
            """
            INSERT INTO arena402.market_projection_receipts (
                result_id,
                task_id,
                task_kind,
                application_outcome
            )
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (result_id) DO UPDATE
            SET result_id = EXCLUDED.result_id
            WHERE market_projection_receipts.task_id = EXCLUDED.task_id
              AND market_projection_receipts.task_kind
                      = EXCLUDED.task_kind
              AND market_projection_receipts.application_outcome
                      = EXCLUDED.application_outcome
            RETURNING result_id
            """,
            row["result_id"],
            row["task_id"],
            row["task_kind"],
            row["application_outcome"],
        )
        if receipt is None:
            raise PawnhouseRepositoryError(
                "agent_market_projection_receipt_conflict"
            )

    @staticmethod
    async def _claim_market_result_locked(
        connection: Any,
        *,
        row: Mapping[str, object],
        action_kind: str,
        action_id: str,
    ) -> None:
        claimed = await connection.fetchrow(
            """
            INSERT INTO arena402.market_result_applications (
                result_id,
                game_id,
                round_id,
                game_participant_id,
                action_kind,
                action_id,
                applied_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT DO NOTHING
            RETURNING
                result_id,
                game_id,
                round_id,
                game_participant_id,
                action_kind,
                action_id
            """,
            row["result_id"],
            row["game_id"],
            row["round_id"],
            row["game_agent_id"],
            action_kind,
            action_id,
            row["authoritative_entered_at"],
        )
        if claimed is None:
            claimed = await connection.fetchrow(
                """
                SELECT
                    result_id,
                    game_id,
                    round_id,
                    game_participant_id,
                    action_kind,
                    action_id
                FROM arena402.market_result_applications
                WHERE result_id = $1
                   OR (
                       action_kind = $2
                       AND action_id = $3
                   )
                """,
                row["result_id"],
                action_kind,
                action_id,
            )
        if claimed is None or (
            claimed["result_id"] != row["result_id"]
            or claimed["game_id"] != row["game_id"]
            or claimed["round_id"] != row["round_id"]
            or claimed["game_participant_id"] != row["game_agent_id"]
            or claimed["action_kind"] != action_kind
            or claimed["action_id"] != action_id
        ):
            raise PawnhouseRepositoryError(
                "agent_market_result_reuse_conflict"
            )

    async def _project_market_intent_locked(
        self,
        connection: Any,
        *,
        row: Mapping[str, object],
        task_input: ArenaMarketIntentInputV1,
        action: MarketIntentActionV1,
    ) -> dict[str, object]:
        if isinstance(action, PassAction):
            return {
                "taskId": str(row["task_id"]),
                "resultId": str(row["result_id"]),
                "kind": str(row["task_kind"]),
                "outcome": "candidate",
                "projected": False,
                "action": "pass",
            }
        if not isinstance(action, (BuyAction, SellAction)):
            raise PawnhouseRepositoryError(
                "agent_market_intent_action_invalid"
            )
        if action.public_price is None or action.limit_price is None:
            raise PawnhouseRepositoryError(
                "agent_market_intent_price_missing"
            )

        intent_id = (
            f"intent:{row['round_id']}:{row['game_agent_id']}"
        )
        await self._claim_market_result_locked(
            connection,
            row=row,
            action_kind="intent",
            action_id=intent_id,
        )
        inserted = await connection.fetchrow(
            """
            INSERT INTO arena402.market_intents (
                intent_id,
                game_id,
                round_id,
                game_participant_id,
                source_result_id,
                source_action_kind,
                side,
                good_id,
                quantity,
                public_price_atomic,
                limit_price_atomic,
                public_message,
                expires_at,
                created_at
            )
            VALUES (
                $1, $2, $3, $4, $5, 'intent', $6, $7, 1,
                $8, $9, $10, $11, $12
            )
            ON CONFLICT (source_result_id) DO UPDATE
            SET source_result_id = EXCLUDED.source_result_id
            WHERE market_intents.intent_id = EXCLUDED.intent_id
              AND market_intents.game_id = EXCLUDED.game_id
              AND market_intents.round_id = EXCLUDED.round_id
              AND market_intents.game_participant_id
                      = EXCLUDED.game_participant_id
              AND market_intents.side = EXCLUDED.side
              AND market_intents.good_id = EXCLUDED.good_id
              AND market_intents.public_price_atomic
                      = EXCLUDED.public_price_atomic
              AND market_intents.limit_price_atomic
                      = EXCLUDED.limit_price_atomic
            RETURNING intent_id
            """,
            intent_id,
            row["game_id"],
            row["round_id"],
            row["game_agent_id"],
            row["result_id"],
            action.action,
            action.good,
            gold(str(action.public_price)),
            gold(str(action.limit_price)),
            action.message,
            task_input.market_expires_at,
            row["authoritative_entered_at"],
        )
        if inserted is None:
            raise PawnhouseRepositoryError(
                "agent_market_intent_idempotency_conflict"
            )
        await self._event(
            connection,
            game_id=str(row["game_id"]),
            round_id=str(row["round_id"]),
            event_type="market.intent_published",
            source_key=str(row["result_id"]),
            public_payload={
                "intentId": intent_id,
                "participantId": str(row["game_agent_id"]),
                "side": action.action,
                "good": action.good,
                "quantity": 1,
                "publicPriceAtomic": gold(str(action.public_price)),
                "message": action.message,
            },
        )
        return {
            "taskId": str(row["task_id"]),
            "resultId": str(row["result_id"]),
            "kind": str(row["task_kind"]),
            "outcome": "candidate",
            "projected": True,
            "intentId": intent_id,
        }

    async def _project_market_rfq_locked(
        self,
        connection: Any,
        *,
        row: Mapping[str, object],
        task_input: ArenaMarketRfqInputV1,
        action: MarketRfqActionV1,
    ) -> dict[str, object]:
        if isinstance(action, PassAction):
            await connection.execute(
                """
                UPDATE arena402.market_rfq_sessions
                SET status = 'stopped',
                    updated_at = clock_timestamp()
                WHERE buyer_intent_id = $1
                  AND game_id = $2
                  AND round_id = $3
                  AND status = 'active'
                """,
                task_input.buyer_intent_id,
                row["game_id"],
                row["round_id"],
            )
            return {
                "taskId": str(row["task_id"]),
                "resultId": str(row["result_id"]),
                "kind": str(row["task_kind"]),
                "outcome": "candidate",
                "projected": True,
                "action": "pass",
            }
        if not isinstance(action, RequestNegotiationsActionV1):
            raise PawnhouseRepositoryError(
                "agent_market_rfq_action_invalid"
            )
        buyer = await connection.fetchrow(
            """
            SELECT intent_id, game_participant_id, good_id, status
            FROM arena402.market_intents
            WHERE intent_id = $1
              AND game_id = $2
              AND round_id = $3
              AND side = 'buy'
            FOR SHARE
            """,
            task_input.buyer_intent_id,
            row["game_id"],
            row["round_id"],
        )
        if (
            buyer is None
            or str(buyer["game_participant_id"])
            != str(row["game_agent_id"])
            or str(buyer["good_id"]) != task_input.good
            or str(buyer["status"]) != "open"
        ):
            raise PawnhouseRepositoryError(
                "agent_market_buyer_intent_invalid"
            )

        session = await connection.fetchrow(
            """
            SELECT *
            FROM arena402.market_rfq_sessions
            WHERE buyer_intent_id = $1
              AND game_id = $2
              AND round_id = $3
            FOR UPDATE
            """,
            task_input.buyer_intent_id,
            row["game_id"],
            row["round_id"],
        )
        if (
            session is None
            or str(session["status"]) != "active"
            or int(session["attempt_count"])
            >= int(session["max_attempts"])
        ):
            raise PawnhouseRepositoryError(
                "agent_market_rfq_budget_exhausted"
            )
        attempt_sequence = int(session["attempt_count"]) + 1
        if task_input.attempt_sequence != attempt_sequence:
            raise PawnhouseRepositoryError(
                "agent_market_rfq_attempt_sequence_invalid"
            )
        unresolved = await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM arena402.market_negotiation_requests
                WHERE buyer_intent_id = $1
                  AND status IN ('pending', 'engaged')
            )
            """,
            task_input.buyer_intent_id,
        )
        if unresolved:
            raise PawnhouseRepositoryError(
                "agent_market_buyer_has_unresolved_rfq"
            )
        frozen = session["frozen_directory"]
        if isinstance(frozen, str):
            frozen = json.loads(frozen)
        frozen_targets = {
            str(entry["intent_id"]) for entry in list(frozen)
        }
        candidate = action.requests[0]
        if candidate.target_intent_id not in frozen_targets:
            raise PawnhouseRepositoryError(
                "agent_market_rfq_target_not_frozen"
            )

        action_id = f"rfq:{row['task_id']}:{attempt_sequence}"
        await self._claim_market_result_locked(
            connection,
            row=row,
            action_kind="rfq",
            action_id=action_id,
        )
        request_ids: list[str] = []
        for sequence, candidate in enumerate(action.requests, start=1):
            seller = await connection.fetchrow(
                """
                SELECT intent_id, game_participant_id, good_id, status
                FROM arena402.market_intents
                WHERE intent_id = $1
                  AND game_id = $2
                  AND round_id = $3
                  AND side = 'sell'
                FOR SHARE
                """,
                candidate.target_intent_id,
                row["game_id"],
                row["round_id"],
            )
            if (
                seller is None
                or str(seller["status"]) != "open"
                or str(seller["good_id"]) != task_input.good
                or str(seller["game_participant_id"])
                == str(row["game_agent_id"])
            ):
                raise PawnhouseRepositoryError(
                    "agent_market_seller_intent_invalid"
                )
            request_id = f"request:{row['task_id']}:{sequence}"
            inserted = await connection.fetchrow(
                """
                INSERT INTO arena402.market_negotiation_requests (
                    request_id,
                    game_id,
                    round_id,
                    buyer_intent_id,
                    seller_intent_id,
                    buyer_participant_id,
                    seller_participant_id,
                    good_id,
                    source_result_id,
                    source_action_kind,
                    opening_price_atomic,
                    public_message,
                    attempt_sequence,
                    created_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8,
                    $9, 'rfq', $10, $11, $12, $13
                )
                ON CONFLICT (request_id) DO UPDATE
                SET request_id = EXCLUDED.request_id
                WHERE market_negotiation_requests.source_result_id
                          = EXCLUDED.source_result_id
                  AND market_negotiation_requests.seller_intent_id
                          = EXCLUDED.seller_intent_id
                  AND market_negotiation_requests.opening_price_atomic
                          = EXCLUDED.opening_price_atomic
                  AND market_negotiation_requests.public_message
                          = EXCLUDED.public_message
                RETURNING request_id
                """,
                request_id,
                row["game_id"],
                row["round_id"],
                task_input.buyer_intent_id,
                candidate.target_intent_id,
                row["game_agent_id"],
                seller["game_participant_id"],
                task_input.good,
                row["result_id"],
                gold(str(candidate.opening_price)),
                candidate.message,
                attempt_sequence,
                row["authoritative_entered_at"],
            )
            if inserted is None:
                raise PawnhouseRepositoryError(
                    "agent_market_rfq_idempotency_conflict"
                )
            request_ids.append(request_id)

        await connection.execute(
            """
            UPDATE arena402.market_rfq_sessions
            SET attempt_count = $2,
                updated_at = clock_timestamp()
            WHERE buyer_intent_id = $1
              AND attempt_count = $2 - 1
            """,
            task_input.buyer_intent_id,
            attempt_sequence,
        )
        await self._event(
            connection,
            game_id=str(row["game_id"]),
            round_id=str(row["round_id"]),
            event_type="market.rfq_sent",
            source_key=str(row["result_id"]),
            public_payload={
                "buyerIntentId": task_input.buyer_intent_id,
                "requestIds": request_ids,
                "count": len(request_ids),
                "attemptSequence": attempt_sequence,
                "remainingAttempts": (
                    int(session["max_attempts"]) - attempt_sequence
                ),
            },
        )
        return {
            "taskId": str(row["task_id"]),
            "resultId": str(row["result_id"]),
            "kind": str(row["task_kind"]),
            "outcome": "candidate",
            "projected": True,
            "requestIds": request_ids,
        }

    async def _project_market_selection_locked(
        self,
        connection: Any,
        *,
        row: Mapping[str, object],
        task_input: ArenaMarketSelectInputV1,
        action: MarketSelectionActionV1,
    ) -> dict[str, object]:
        if isinstance(action, RejectAllRequestsActionV1):
            action_id = f"reject-all:{row['task_id']}"
            await self._claim_market_result_locked(
                connection,
                row=row,
                action_kind="reject",
                action_id=action_id,
            )
            request_ids = [
                request.request_id for request in task_input.requests
            ]
            await connection.execute(
                """
                UPDATE arena402.market_negotiation_requests
                SET status = 'rejected'
                WHERE request_id = ANY($1::text[])
                  AND game_id = $2
                  AND round_id = $3
                  AND seller_participant_id = $4
                  AND status = 'pending'
                """,
                request_ids,
                row["game_id"],
                row["round_id"],
                row["game_agent_id"],
            )
            await self._event(
                connection,
                game_id=str(row["game_id"]),
                round_id=str(row["round_id"]),
                event_type="market.rfq_rejected",
                source_key=str(row["result_id"]),
                public_payload={
                    "sellerIntentId": task_input.seller_intent_id,
                    "requestIds": request_ids,
                    "message": action.message,
                },
            )
            return {
                "taskId": str(row["task_id"]),
                "resultId": str(row["result_id"]),
                "kind": str(row["task_kind"]),
                "outcome": "candidate",
                "projected": True,
                "rejectedRequestIds": request_ids,
            }
        if not isinstance(action, EngageRequestActionV1):
            raise PawnhouseRepositoryError(
                "agent_market_selection_action_invalid"
            )

        request = await connection.fetchrow(
            """
            SELECT
                request.*,
                buyer.limit_price_atomic
                    AS buyer_limit_price_atomic,
                seller.limit_price_atomic
                    AS seller_limit_price_atomic,
                buyer_balance.cash_atomic AS buyer_cash_atomic,
                seller_holding.quantity AS seller_inventory
            FROM arena402.market_negotiation_requests AS request
            JOIN arena402.market_intents AS buyer
              ON buyer.intent_id = request.buyer_intent_id
            JOIN arena402.market_intents AS seller
              ON seller.intent_id = request.seller_intent_id
            JOIN arena402.balances AS buyer_balance
              ON buyer_balance.game_participant_id =
                 request.buyer_participant_id
            JOIN arena402.holdings AS seller_holding
              ON seller_holding.game_participant_id =
                 request.seller_participant_id
             AND seller_holding.good_id = request.good_id
            WHERE request.request_id = $1
              AND request.game_id = $2
              AND request.round_id = $3
              AND buyer.status = 'open'
              AND seller.status = 'open'
              AND buyer.expires_at > clock_timestamp()
              AND seller.expires_at > clock_timestamp()
            FOR UPDATE OF request
            """,
            action.request_id,
            row["game_id"],
            row["round_id"],
        )
        if (
            request is None
            or str(request["seller_participant_id"])
            != str(row["game_agent_id"])
        ):
            raise PawnhouseRepositoryError(
                "agent_market_request_ownership_invalid"
            )
        engagement_id = f"engagement:{action.request_id}"
        existing = await connection.fetchrow(
            """
            SELECT engagement_id, selection_result_id
            FROM arena402.market_engagements
            WHERE engagement_id = $1
            """,
            engagement_id,
        )
        if existing is not None:
            if str(existing["selection_result_id"]) != str(
                row["result_id"]
            ):
                raise PawnhouseRepositoryError(
                    "agent_market_engagement_idempotency_conflict"
                )
            return {
                "taskId": str(row["task_id"]),
                "resultId": str(row["result_id"]),
                "kind": str(row["task_kind"]),
                "outcome": "candidate",
                "projected": True,
                "engagementId": engagement_id,
            }
        if str(request["status"]) != "pending":
            raise PawnhouseRepositoryError(
                "agent_market_request_not_pending"
            )
        if (
            int(request["buyer_limit_price_atomic"])
            < int(request["seller_limit_price_atomic"])
            or int(request["buyer_cash_atomic"])
            < int(request["opening_price_atomic"])
            or int(request["seller_inventory"]) < 1
        ):
            raise PawnhouseRepositoryError(
                "agent_market_engagement_assets_invalid"
            )

        participants = sorted(
            (
                str(request["buyer_participant_id"]),
                str(request["seller_participant_id"]),
            )
        )
        for participant_id in participants:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                (
                    f"agent-market-slot:{row['round_id']}:"
                    f"{participant_id}"
                ),
            )
        await self._claim_market_result_locked(
            connection,
            row=row,
            action_kind="engage",
            action_id=engagement_id,
        )
        participant_busy = await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM arena402.participant_round_slots
                WHERE round_id = $1
                  AND game_participant_id = ANY($2::text[])
                  AND status IN ('reserved', 'consumed')
            )
            """,
            row["round_id"],
            participants,
        )
        if participant_busy:
            await connection.execute(
                """
                UPDATE arena402.market_negotiation_requests
                SET status = 'counterparty_busy'
                WHERE request_id = $1
                  AND status = 'pending'
                """,
                action.request_id,
            )
            await self._event(
                connection,
                game_id=str(row["game_id"]),
                round_id=str(row["round_id"]),
                event_type="market.rfq_busy",
                source_key=str(row["result_id"]),
                public_payload={
                    "requestId": action.request_id,
                    "status": "counterparty_busy",
                },
            )
            return {
                "taskId": str(row["task_id"]),
                "resultId": str(row["result_id"]),
                "kind": str(row["task_kind"]),
                "outcome": "candidate",
                "projected": True,
                "requestId": action.request_id,
                "status": "counterparty_busy",
            }
        await connection.execute(
            """
            INSERT INTO arena402.market_engagements (
                engagement_id,
                negotiation_id,
                request_id,
                game_id,
                round_id,
                buyer_intent_id,
                seller_intent_id,
                buyer_participant_id,
                seller_participant_id,
                good_id,
                selection_result_id,
                selection_action_kind
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, 'engage'
            )
            """,
            engagement_id,
            f"negotiation:{action.request_id}",
            action.request_id,
            row["game_id"],
            row["round_id"],
            request["buyer_intent_id"],
            request["seller_intent_id"],
            request["buyer_participant_id"],
            request["seller_participant_id"],
            request["good_id"],
            row["result_id"],
        )
        for participant_id in participants:
            reserved = await connection.fetchrow(
                """
                INSERT INTO arena402.participant_round_slots (
                    game_id,
                    round_id,
                    game_participant_id,
                    status,
                    engagement_id
                )
                VALUES ($1, $2, $3, 'reserved', $4)
                ON CONFLICT (round_id, game_participant_id) DO UPDATE
                SET status = 'reserved',
                    engagement_id = EXCLUDED.engagement_id,
                    version = participant_round_slots.version + 1,
                    updated_at = clock_timestamp()
                WHERE participant_round_slots.status = 'available'
                   OR (
                       participant_round_slots.status = 'reserved'
                       AND participant_round_slots.engagement_id
                               = EXCLUDED.engagement_id
                   )
                RETURNING game_participant_id
                """,
                row["game_id"],
                row["round_id"],
                participant_id,
                engagement_id,
            )
            if reserved is None:
                raise PawnhouseRepositoryError(
                    "agent_market_participant_busy"
                )
        updated = await connection.fetchrow(
            """
            UPDATE arena402.market_negotiation_requests
            SET status = 'engaged'
            WHERE request_id = $1
              AND status = 'pending'
            RETURNING request_id
            """,
            action.request_id,
        )
        if updated is None:
            raise PawnhouseRepositoryError(
                "agent_market_request_not_pending"
            )
        await connection.execute(
            """
            UPDATE arena402.market_intents
            SET status = 'reserved'
            WHERE intent_id IN ($1, $2)
              AND status = 'open'
            """,
            request["buyer_intent_id"],
            request["seller_intent_id"],
        )
        await self._event(
            connection,
            game_id=str(row["game_id"]),
            round_id=str(row["round_id"]),
            event_type="market.engagement_created",
            source_key=str(row["result_id"]),
            public_payload={
                "engagementId": engagement_id,
                "requestId": action.request_id,
                "buyerParticipantId": str(
                    request["buyer_participant_id"]
                ),
                "sellerParticipantId": str(
                    request["seller_participant_id"]
                ),
                "good": str(request["good_id"]),
            },
        )
        return {
            "taskId": str(row["task_id"]),
            "resultId": str(row["result_id"]),
            "kind": str(row["task_kind"]),
            "outcome": "candidate",
            "projected": True,
            "engagementId": engagement_id,
        }

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
        quantity = action.get("quantity", 1)
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            quantity = 1
        quantity = max(1, min(1_000_000, quantity))
        limit_price_atomic = None
        if action.get("limitPrice", action.get("limit_price")) is not None:
            try:
                limit_price_atomic = gold(
                    str(action.get("limitPrice", action.get("limit_price")))
                )
            except (TypeError, ValueError):
                limit_price_atomic = None
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
                        "quantity": quantity,
                        "limitPriceAtomic": limit_price_atomic,
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
                    if available is None or available < quantity:
                        action_name = "pass"
                        good_id = None
                        quantity = 1
                        limit_price_atomic = None
                if action_name != "pass":
                    await connection.execute(
                        """
                        INSERT INTO arena402.pool_entries (
                            pool_entry_id, game_id, round_id,
                            game_participant_id, source_result_id,
                            side, good_id, quantity, limit_price_atomic,
                            result_received_at
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                        ON CONFLICT (source_result_id) DO NOTHING
                        """,
                        f"pool:{round_id}:{participant_id}",
                        game_id,
                        round_id,
                        participant_id,
                        result_id,
                        action_name,
                        good_id,
                        quantity,
                        limit_price_atomic,
                        result_received_at,
                    )
                public = {
                    "participantId": participant_id,
                    "action": action_name,
                    "good": good_id,
                    "quantity": quantity,
                    "limitPriceAtomic": limit_price_atomic,
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
                SELECT
                    p.agent_id,
                    a.name,
                    (
                        SELECT count(*)
                        FROM arena402.pairings AS prior_pairing
                        WHERE prior_pairing.game_id = p.game_id
                          AND prior_pairing.status IN ('rejected', 'timeout')
                          AND (
                              prior_pairing.buyer_participant_id =
                                  p.game_participant_id
                              OR prior_pairing.seller_participant_id =
                                  p.game_participant_id
                          )
                    ) AS failed_negotiations
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
            pairing = await connection.fetchrow(
                """
                SELECT quantity, buyer_limit_price_atomic,
                       seller_limit_price_atomic
                FROM arena402.pairings
                WHERE pairing_id = $1
                """,
                negotiation["pairing_id"],
            )
            round_context = await connection.fetchrow(
                """
                SELECT round.round_index, game.round_count
                FROM arena402.rounds AS round
                JOIN arena402.games AS game
                  ON game.game_id = round.game_id
                WHERE round.round_id = $1
                """,
                negotiation["round_id"],
            )
            if round_context is None:
                raise PawnhouseRepositoryError("negotiation_round_missing")
            round_index = int(round_context["round_index"])
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
            "round_count": int(round_context["round_count"]),
            "rounds_remaining": (
                int(round_context["round_count"]) - round_index
            ),
            "negotiation_id": negotiation_id,
            "participant_id": participant_id,
            "counterparty_id": counterparty_id,
            "counterparty_agent_id": counterparty["agent_id"],
            "counterparty_name": counterparty["name"] or "Arena Agent",
            "counterparty_failed_negotiations": int(
                counterparty["failed_negotiations"]
            ),
            "role": role,
            "good": good_id,
            "quantity": int(pairing["quantity"]),
            "limit_price_atomic": (
                pairing["buyer_limit_price_atomic"]
                if role == "buyer"
                else pairing["seller_limit_price_atomic"]
            ),
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
                    pairing = await connection.fetchrow(
                        """
                        SELECT quantity, buyer_limit_price_atomic,
                               seller_limit_price_atomic
                        FROM arena402.pairings
                        WHERE pairing_id = $1
                        """,
                        row["pairing_id"],
                    )
                    own_limit = (
                        pairing["buyer_limit_price_atomic"]
                        if row["next_role"] == "buyer"
                        else pairing["seller_limit_price_atomic"]
                    )
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
                    quote_price = action_value.price_atomic
                    if action_name == "accept" and negotiation.turns:
                        quote_price = negotiation.turns[-1].action.price_atomic
                    if own_limit is not None and quote_price is not None:
                        outside_limit = (
                            row["next_role"] == "buyer"
                            and quote_price > int(own_limit)
                        ) or (
                            row["next_role"] == "seller"
                            and quote_price < int(own_limit)
                        )
                        if outside_limit:
                            action_value = NegotiationAction(
                                action="reject",
                                message="The proposed price is outside my limit.",
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
                        completed_at = $8,
                        action_deadline_at = CASE
                            WHEN $4::text = 'active'
                            THEN clock_timestamp() + (
                                SELECT action_timeout_ms
                                FROM arena402.games
                                WHERE game_id = $9
                            ) * interval '1 millisecond'
                            ELSE action_deadline_at
                        END
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
                    row["game_id"],
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
                    await self._sync_agent_market_negotiation_locked(
                        connection,
                        negotiation_id=negotiation_id,
                        terminal_result_id=result_id,
                        agent_authored=action is not None,
                    )
                    if negotiation.status in {
                        NegotiationStatus.REJECTED,
                        NegotiationStatus.TIMEOUT,
                    }:
                        await self._pairing_closed_event(
                            connection,
                            game_id=row["game_id"],
                            round_id=row["round_id"],
                            pairing_id=row["pairing_id"],
                            negotiation_id=negotiation_id,
                            status=negotiation.status.value,
                        )
                    if (
                        negotiation.status
                        is NegotiationStatus.ACCEPTED_PENDING_SETTLEMENT
                    ):
                        intent = await self._freeze_settlement_intent(
                            connection,
                            negotiation_id=negotiation_id,
                        )
                        if intent is None:
                            negotiation.status = (
                                NegotiationStatus.SETTLEMENT_FAILED
                            )
                            await connection.execute(
                                """
                                UPDATE arena402.market_engagements
                                SET status = 'settlement_failed',
                                    completed_at = COALESCE(
                                        completed_at,
                                        clock_timestamp()
                                    )
                                WHERE negotiation_id = $1
                                  AND status =
                                      'accepted_pending_settlement'
                                """,
                                negotiation_id,
                            )
                        else:
                            await connection.execute(
                                """
                                UPDATE arena402.market_engagements
                                SET status = 'settling'
                                WHERE negotiation_id = $1
                                  AND status =
                                      'accepted_pending_settlement'
                                """,
                                negotiation_id,
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

    async def _sync_agent_market_negotiation_locked(
        self,
        connection: Any,
        *,
        negotiation_id: str,
        terminal_result_id: str,
        agent_authored: bool,
    ) -> None:
        """Project terminal negotiation provenance onto its Engagement."""

        row = await connection.fetchrow(
            """
            SELECT
                engagement.*,
                negotiation.status AS negotiation_status,
                negotiation.accepted_price_atomic,
                request.source_result_id AS rfq_result_id
            FROM arena402.market_engagements AS engagement
            JOIN arena402.negotiations AS negotiation
              ON negotiation.negotiation_id =
                 engagement.negotiation_id
            JOIN arena402.market_negotiation_requests AS request
              ON request.request_id = engagement.request_id
            WHERE engagement.negotiation_id = $1
            FOR UPDATE OF engagement
            """,
            negotiation_id,
        )
        if row is None:
            return
        status = str(row["negotiation_status"])
        if status == "accepted_pending_settlement":
            proposal = await connection.fetchrow(
                """
                SELECT
                    source_result_id,
                    turn_sequence,
                    actor_role
                FROM arena402.negotiation_messages
                WHERE negotiation_id = $1
                  AND action = 'propose'
                ORDER BY turn_sequence DESC
                LIMIT 1
                """,
                negotiation_id,
            )
            acceptance = await connection.fetchrow(
                """
                SELECT source_result_id, actor_role
                FROM arena402.negotiation_messages
                WHERE negotiation_id = $1
                  AND source_result_id = $2
                  AND action = 'accept'
                """,
                negotiation_id,
                terminal_result_id,
            )
            if (
                proposal is None
                or acceptance is None
                or row["accepted_price_atomic"] is None
            ):
                raise PawnhouseRepositoryError(
                    "agent_market_deal_provenance_missing"
                )
            proposal_action_id = (
                f"proposal:{row['engagement_id']}:"
                f"{proposal['turn_sequence']}"
            )
            acceptance_action_id = (
                f"acceptance:{row['engagement_id']}"
            )
            proposal_is_binding_rfq = (
                str(proposal["source_result_id"])
                == str(row["rfq_result_id"])
                and int(proposal["turn_sequence"]) == 1
            )
            if not proposal_is_binding_rfq:
                await self._claim_market_negotiation_result_locked(
                    connection,
                    result_id=str(proposal["source_result_id"]),
                    game_id=str(row["game_id"]),
                    round_id=str(row["round_id"]),
                    participant_id=(
                        str(row["buyer_participant_id"])
                        if str(proposal["actor_role"]) == "buyer"
                        else str(row["seller_participant_id"])
                    ),
                    action_kind="proposal",
                    action_id=proposal_action_id,
                )
            await self._claim_market_negotiation_result_locked(
                connection,
                result_id=str(acceptance["source_result_id"]),
                game_id=str(row["game_id"]),
                round_id=str(row["round_id"]),
                participant_id=(
                    str(row["buyer_participant_id"])
                    if str(acceptance["actor_role"]) == "buyer"
                    else str(row["seller_participant_id"])
                ),
                action_kind="acceptance",
                action_id=acceptance_action_id,
            )
            deal_id = f"deal:{row['engagement_id']}"
            await connection.execute(
                """
                INSERT INTO arena402.market_deals (
                    deal_id,
                    engagement_id,
                    request_id,
                    game_id,
                    round_id,
                    buyer_participant_id,
                    seller_participant_id,
                    good_id,
                    quantity,
                    unit_price_atomic,
                    latest_proposal_result_id,
                    latest_proposal_action_kind,
                    latest_proposal_request_id,
                    acceptance_result_id,
                    acceptance_action_kind,
                    accepted_by_participant_id
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, 1, $9,
                    $10, $11, $12, $13, 'acceptance', $14
                )
                ON CONFLICT (engagement_id) DO NOTHING
                """,
                deal_id,
                row["engagement_id"],
                row["request_id"],
                row["game_id"],
                row["round_id"],
                row["buyer_participant_id"],
                row["seller_participant_id"],
                row["good_id"],
                row["accepted_price_atomic"],
                proposal["source_result_id"],
                (
                    "rfq"
                    if proposal_is_binding_rfq
                    else "proposal"
                ),
                (
                    row["request_id"]
                    if proposal_is_binding_rfq
                    else None
                ),
                acceptance["source_result_id"],
                (
                    row["buyer_participant_id"]
                    if str(acceptance["actor_role"]) == "buyer"
                    else row["seller_participant_id"]
                ),
            )
            await connection.execute(
                """
                UPDATE arena402.market_engagements
                SET status = 'accepted_pending_settlement',
                    terminal_source_result_id = $2,
                    terminal_action_kind = 'acceptance'
                WHERE negotiation_id = $1
                  AND status = 'active'
                """,
                negotiation_id,
                acceptance["source_result_id"],
            )
            await connection.execute(
                """
                UPDATE arena402.participant_round_slots
                SET status = 'consumed',
                    version = version + 1,
                    updated_at = clock_timestamp()
                WHERE engagement_id = $1
                  AND status = 'reserved'
                """,
                row["engagement_id"],
            )
            await connection.execute(
                """
                UPDATE arena402.market_rfq_sessions
                SET status = 'completed',
                    updated_at = clock_timestamp()
                WHERE buyer_intent_id = $1
                  AND status = 'active'
                """,
                row["buyer_intent_id"],
            )
            await self._event(
                connection,
                game_id=str(row["game_id"]),
                round_id=str(row["round_id"]),
                event_type="market.deal_frozen",
                source_key=f"{deal_id}:frozen",
                public_payload={
                    "dealId": deal_id,
                    "engagementId": str(row["engagement_id"]),
                    "negotiationId": negotiation_id,
                    "good": str(row["good_id"]),
                    "quantity": 1,
                    "unitPriceAtomic": str(
                        row["accepted_price_atomic"]
                    ),
                },
            )
            return

        if status not in {"rejected", "timeout"}:
            return
        engagement_status = (
            "rejected" if status == "rejected" else "timed_out"
        )
        terminal_kind = None
        terminal_result = None
        if agent_authored and status == "rejected":
            terminal_kind = "reject"
            terminal_result = terminal_result_id
            await self._claim_market_negotiation_result_locked(
                connection,
                result_id=terminal_result_id,
                game_id=str(row["game_id"]),
                round_id=str(row["round_id"]),
                participant_id=(
                    str(row["buyer_participant_id"])
                    if await connection.fetchval(
                        """
                        SELECT actor_role = 'buyer'
                        FROM arena402.negotiation_messages
                        WHERE source_result_id = $1
                        """,
                        terminal_result_id,
                    )
                    else str(row["seller_participant_id"])
                ),
                action_kind="reject",
                action_id=f"reject:{row['engagement_id']}",
            )
        await connection.execute(
            """
            UPDATE arena402.market_engagements
            SET status = $2,
                terminal_source_result_id = $3,
                terminal_action_kind = $4,
                completed_at = COALESCE(
                    completed_at,
                    clock_timestamp()
                )
            WHERE negotiation_id = $1
              AND status = 'active'
            """,
            negotiation_id,
            engagement_status,
            terminal_result,
            terminal_kind,
        )
        await connection.execute(
            """
            UPDATE arena402.participant_round_slots
            SET status = 'available',
                engagement_id = NULL,
                version = version + 1,
                updated_at = clock_timestamp()
            WHERE engagement_id = $1
              AND status = 'reserved'
            """,
            row["engagement_id"],
        )
        await connection.execute(
            """
            UPDATE arena402.market_negotiation_requests
            SET status = $2
            WHERE request_id = $1
              AND status = 'engaged'
            """,
            row["request_id"],
            (
                "expired"
                if engagement_status == "timed_out"
                else "rejected"
            ),
        )
        await connection.execute(
            """
            UPDATE arena402.market_intents
            SET status = 'open'
            WHERE intent_id IN ($1, $2)
              AND status = 'reserved'
              AND expires_at > clock_timestamp()
            """,
            row["buyer_intent_id"],
            row["seller_intent_id"],
        )

    @staticmethod
    async def _claim_market_negotiation_result_locked(
        connection: Any,
        *,
        result_id: str,
        game_id: str,
        round_id: str,
        participant_id: str,
        action_kind: str,
        action_id: str,
    ) -> None:
        claimed = await connection.fetchrow(
            """
            INSERT INTO arena402.market_result_applications (
                result_id,
                game_id,
                round_id,
                game_participant_id,
                action_kind,
                action_id,
                applied_at
            )
            SELECT
                result.result_id,
                $2,
                $3,
                $4,
                $5,
                $6,
                result.result_received_at
            FROM public.arena_agent_task_results AS result
            WHERE result.result_id = $1
            ON CONFLICT (result_id) DO UPDATE
            SET result_id = EXCLUDED.result_id
            WHERE market_result_applications.game_id =
                      EXCLUDED.game_id
              AND market_result_applications.round_id =
                      EXCLUDED.round_id
              AND market_result_applications.game_participant_id =
                      EXCLUDED.game_participant_id
              AND market_result_applications.action_kind =
                      EXCLUDED.action_kind
              AND market_result_applications.action_id =
                      EXCLUDED.action_id
            RETURNING result_id
            """,
            result_id,
            game_id,
            round_id,
            participant_id,
            action_kind,
            action_id,
        )
        if claimed is None:
            raise PawnhouseRepositoryError(
                "agent_market_negotiation_result_invalid"
            )

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

    async def ledger_trades(
        self,
        *,
        game_id: str | None = None,
        agent_id: str | None = None,
        good_id: str | None = None,
        after_created_at: datetime | None = None,
        after_trade_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 101:
            raise ValueError("limit must be between 1 and 101")
        if (after_created_at is None) != (after_trade_id is None):
            raise ValueError("ledger cursor fields must be provided together")
        rows = await self._require_pool().fetch(
            """
            SELECT
                i.settlement_intent_id,
                i.game_id,
                round_row.round_index,
                i.good_id,
                i.quantity,
                i.unit_price_atomic,
                i.amount_atomic,
                i.buyer_agent_id,
                coalesce(
                    buyer_agent.name,
                    i.buyer_agent_id
                ) AS buyer_display_name,
                i.buyer_account,
                i.seller_agent_id,
                coalesce(
                    seller_agent.name,
                    i.seller_agent_id
                ) AS seller_display_name,
                i.seller_account,
                i.pairing_id,
                i.chain_id,
                i.status,
                i.created_at,
                i.chain_confirmed_at,
                submission.tx_hash,
                confirmation.block_number,
                confirmation.observed_at AS confirmation_observed_at,
                confirmation.facilitator_address
            FROM arena402.settlement_intents AS i
            JOIN arena402.rounds AS round_row
              ON round_row.round_id = i.round_id
             AND round_row.game_id = i.game_id
            LEFT JOIN public.arena_agents AS buyer_agent
              ON buyer_agent.agent_id = i.buyer_agent_id
            LEFT JOIN public.arena_agents AS seller_agent
              ON seller_agent.agent_id = i.seller_agent_id
            LEFT JOIN arena402.settlement_submissions AS submission
              ON submission.settlement_intent_id = i.settlement_intent_id
            LEFT JOIN arena402.settlement_confirmations AS confirmation
              ON confirmation.settlement_intent_id = i.settlement_intent_id
            WHERE ($1::text IS NULL OR i.game_id = $1)
              AND (
                  $2::text IS NULL
                  OR i.buyer_agent_id = $2
                  OR i.seller_agent_id = $2
              )
              AND ($3::text IS NULL OR i.good_id = $3)
              AND (
                  $4::timestamptz IS NULL
                  OR (i.created_at, i.settlement_intent_id)
                     < ($4::timestamptz, $5::text)
              )
            ORDER BY i.created_at DESC, i.settlement_intent_id DESC
            LIMIT $6
            """,
            game_id,
            agent_id,
            good_id,
            after_created_at,
            after_trade_id,
            limit,
        )
        return [self._ledger_trade_public(row) for row in rows]

    async def ledger_stats(self) -> dict[str, object]:
        row = await self._require_pool().fetchrow(
            """
            WITH confirmed AS MATERIALIZED (
                SELECT
                    i.amount_atomic,
                    i.buyer_agent_id,
                    i.seller_agent_id
                FROM arena402.settlement_intents AS i
                JOIN arena402.settlement_confirmations AS confirmation
                  ON confirmation.settlement_intent_id =
                     i.settlement_intent_id
            )
            SELECT
                count(*) AS trade_count,
                coalesce(sum(amount_atomic), 0) AS volume_atomic,
                (
                    SELECT count(*)
                    FROM (
                        SELECT buyer_agent_id AS agent_id FROM confirmed
                        UNION
                        SELECT seller_agent_id AS agent_id FROM confirmed
                    ) AS agents
                ) AS agent_count
            FROM confirmed
            """
        )
        assert row is not None
        return {
            "totalTrades": int(row["trade_count"]),
            "totalAmountAtomic": str(int(row["volume_atomic"])),
            "agentCount": int(row["agent_count"]),
        }

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
                    await connection.execute(
                        """
                        UPDATE arena402.pairings
                        SET status = 'settlement_failed',
                            completed_at = COALESCE(
                                completed_at,
                                clock_timestamp()
                            )
                        WHERE pairing_id = $1
                          AND status <> 'settlement_failed'
                        """,
                        intent["pairing_id"],
                    )
                    await connection.execute(
                        """
                        UPDATE arena402.negotiations
                        SET status = 'settlement_failed',
                            completed_at = COALESCE(
                                completed_at,
                                clock_timestamp()
                            )
                        WHERE negotiation_id = $1
                          AND status = 'accepted_pending_settlement'
                        """,
                        intent["negotiation_id"],
                    )
                    await self._pairing_closed_event(
                        connection,
                        game_id=intent["game_id"],
                        round_id=intent["round_id"],
                        pairing_id=intent["pairing_id"],
                        negotiation_id=intent["negotiation_id"],
                        status="settlement_failed",
                        settlement_intent_id=settlement_intent_id,
                    )
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
                    UPDATE arena402.negotiations
                    SET status = 'settlement_failed',
                        completed_at = COALESCE(
                            completed_at,
                            clock_timestamp()
                        )
                    WHERE negotiation_id = $1
                      AND status = 'accepted_pending_settlement'
                    """,
                    intent["negotiation_id"],
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
                        "pairingId": intent["pairing_id"],
                        "negotiationId": intent["negotiation_id"],
                        "status": "authorization_failed",
                        "safeErrorCode": safe_error_code,
                    },
                )
                await self._pairing_closed_event(
                    connection,
                    game_id=intent["game_id"],
                    round_id=intent["round_id"],
                    pairing_id=intent["pairing_id"],
                    negotiation_id=intent["negotiation_id"],
                    status="settlement_failed",
                    settlement_intent_id=settlement_intent_id,
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
                            facilitator_address,
                            token_address, from_account, to_account,
                            amount_atomic, block_number, block_hash,
                            confirmation_count, evidence_hash
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                            $11, $12
                        )
                        """,
                        settlement_intent_id,
                        confirmation.tx_hash,
                        confirmation.chain_id,
                        confirmation.facilitator_address,
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
                            "facilitatorAddress": (
                                confirmation.facilitator_address
                            ),
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
                        "facilitator_address": (
                            confirmation.facilitator_address
                        ),
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
                    await connection.execute(
                        """
                        UPDATE arena402.pairings
                        SET status = 'settlement_failed',
                            completed_at = COALESCE(
                                completed_at,
                                clock_timestamp()
                            )
                        WHERE pairing_id = $1
                          AND status <> 'settlement_failed'
                        """,
                        row["pairing_id"],
                    )
                    await connection.execute(
                        """
                        UPDATE arena402.negotiations
                        SET status = 'settlement_failed',
                            completed_at = COALESCE(
                                completed_at,
                                clock_timestamp()
                            )
                        WHERE negotiation_id = $1
                          AND status = 'accepted_pending_settlement'
                        """,
                        row["negotiation_id"],
                    )
                    await self._pairing_closed_event(
                        connection,
                        game_id=row["game_id"],
                        round_id=row["round_id"],
                        pairing_id=row["pairing_id"],
                        negotiation_id=row["negotiation_id"],
                        status="settlement_failed",
                        settlement_intent_id=settlement_intent_id,
                    )
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
                    UPDATE arena402.negotiations
                    SET status = 'settlement_failed',
                        completed_at = COALESCE(
                            completed_at,
                            clock_timestamp()
                        )
                    WHERE negotiation_id = $1
                      AND status = 'accepted_pending_settlement'
                    """,
                    row["negotiation_id"],
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
                        "pairingId": row["pairing_id"],
                        "negotiationId": row["negotiation_id"],
                        "txHash": normalized_tx,
                        "status": "reverted",
                    },
                )
                await self._pairing_closed_event(
                    connection,
                    game_id=row["game_id"],
                    round_id=row["round_id"],
                    pairing_id=row["pairing_id"],
                    negotiation_id=row["negotiation_id"],
                    status="settlement_failed",
                    settlement_intent_id=settlement_intent_id,
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
                    await connection.execute(
                        """
                        UPDATE arena402.pairings
                        SET status = 'settled',
                            completed_at = COALESCE(
                                completed_at,
                                clock_timestamp()
                            )
                        WHERE pairing_id = $1
                          AND status <> 'settled'
                        """,
                        intent["pairing_id"],
                    )
                    await connection.execute(
                        """
                        UPDATE arena402.negotiations
                        SET status = 'settled',
                            completed_at = COALESCE(
                                completed_at,
                                clock_timestamp()
                            )
                        WHERE negotiation_id = $1
                          AND status = 'accepted_pending_settlement'
                        """,
                        intent["negotiation_id"],
                    )
                    await self._pairing_closed_event(
                        connection,
                        game_id=intent["game_id"],
                        round_id=intent["round_id"],
                        pairing_id=intent["pairing_id"],
                        negotiation_id=intent["negotiation_id"],
                        status="settled",
                        settlement_intent_id=settlement_intent_id,
                    )
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
                quantity = int(intent["quantity"])
                if seller_holding_before < quantity:
                    raise PawnhouseRepositoryError(
                        "seller_inventory_changed_before_commit"
                    )
                buyer_cash_after = buyer_cash_before - amount
                seller_cash_after = seller_cash_before + amount
                buyer_holding_after = buyer_holding_before + quantity
                seller_holding_after = seller_holding_before - quantity
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
                    UPDATE arena402.negotiations
                    SET status = 'settled',
                        completed_at = COALESCE(
                            completed_at,
                            clock_timestamp()
                        )
                    WHERE negotiation_id = $1
                      AND status = 'accepted_pending_settlement'
                    """,
                    intent["negotiation_id"],
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
                await self._pairing_closed_event(
                    connection,
                    game_id=intent["game_id"],
                    round_id=intent["round_id"],
                    pairing_id=intent["pairing_id"],
                    negotiation_id=intent["negotiation_id"],
                    status="settled",
                    settlement_intent_id=settlement_intent_id,
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

    async def actionable_game_actions(
        self,
        *,
        limit: int = 50,
    ) -> list[dict[str, str]]:
        """Discover currently executable transitions in one set-based query."""

        if limit < 1 or limit > 500:
            raise ValueError("automation game limit must be between 1 and 500")
        rows = await self._require_pool().fetch(
            """
            WITH game_state AS (
                SELECT
                    game.game_id,
                    game.started_at,
                    game.market_protocol,
                    round.round_id,
                    round.phase AS round_phase,
                    runtime.participant_count,
                    runtime.all_rule,
                    runtime.all_task_runtime,
                    runtime_run.status AS runtime_run_status,
                    active_negotiation.present AS active_negotiation,
                    pending_settlement.present AS pending_settlement
                FROM arena402.games AS game
                JOIN arena402.rounds AS round
                  ON round.game_id = game.game_id
                 AND round.round_index = game.current_round
                LEFT JOIN LATERAL (
                    SELECT
                        count(*) AS participant_count,
                        bool_and(
                            participant.runtime_kind = 'rule'
                        ) AS all_rule,
                        bool_and(
                            participant.runtime_kind IN (
                                'hosted',
                                'connector'
                            )
                        ) AS all_task_runtime
                    FROM arena402.game_participants AS participant
                    WHERE participant.game_id = game.game_id
                      AND participant.status IN ('active', 'settling')
                ) AS runtime ON TRUE
                LEFT JOIN LATERAL (
                    SELECT run.status
                    FROM arena402.runtime_runs AS run
                    WHERE run.round_id = round.round_id
                      AND run.runtime_kind IN ('hosted', 'mixed')
                    ORDER BY run.runtime_kind
                    LIMIT 1
                ) AS runtime_run ON TRUE
                LEFT JOIN LATERAL (
                    SELECT TRUE AS present
                    FROM arena402.negotiations AS negotiation
                    WHERE negotiation.round_id = round.round_id
                      AND negotiation.status = 'active'
                    LIMIT 1
                ) AS active_negotiation ON TRUE
                LEFT JOIN LATERAL (
                    SELECT TRUE AS present
                    FROM arena402.pairings AS pairing
                    WHERE pairing.round_id = round.round_id
                      AND pairing.status IN (
                          'accepted_pending_settlement',
                          'settling'
                      )
                    LIMIT 1
                ) AS pending_settlement ON TRUE
                WHERE game.phase = 'running'
            ),
            actionable AS (
                SELECT
                    game_id,
                    started_at,
                    CASE
                        WHEN round_phase = 'decide'
                         AND market_protocol = 'fcfs.v1'
                         AND participant_count > 0
                         AND all_rule
                        THEN 'run_rule'
                        WHEN round_phase = 'decide'
                         AND participant_count > 0
                         AND all_task_runtime
                         AND runtime_run_status IS NULL
                        THEN 'enqueue_agent_runtime'
                        WHEN round_phase IN (
                            'negotiate',
                            'settle',
                            'round_close'
                        )
                         AND NOT (
                            participant_count > 0
                            AND all_task_runtime
                            AND (
                                runtime_run_status IS NULL
                                OR runtime_run_status <> 'completed'
                            )
                         )
                         AND active_negotiation IS NOT TRUE
                         AND pending_settlement IS NOT TRUE
                        THEN 'advance_round'
                        ELSE NULL
                    END AS action
                FROM game_state
            )
            SELECT game_id, action
            FROM actionable
            WHERE action IS NOT NULL
            ORDER BY started_at NULLS LAST, game_id
            LIMIT $1
            """,
            limit,
        )
        return [
            {
                "gameId": str(row["game_id"]),
                "action": str(row["action"]),
            }
            for row in rows
        ]

    async def automation_state(
        self,
        *,
        game_id: str,
    ) -> dict[str, object]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            game = await connection.fetchrow(
                """
                SELECT phase, current_round, market_protocol
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
            if (
                game["market_protocol"] == "fcfs.v1"
                and set(runtime_kinds) == {"rule"}
            ):
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
            "marketProtocol": str(game["market_protocol"]),
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
                        event_schedule_commitment, market_protocol,
                        config_snapshot
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

                if str(game["market_protocol"]) == "agent_a2a.v1":
                    await self._record_agent_market_liquidity_summary(
                        connection,
                        game_id=game_id,
                        round_id=round_id,
                    )
                await self._terminalize_agent_market_scope(
                    connection,
                    game_id=game_id,
                    round_id=round_id,
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
                    game_config = (
                        json.loads(game["config_snapshot"])
                        if isinstance(game["config_snapshot"], str)
                        else dict(game["config_snapshot"])
                    )
                    try:
                        frozen_price_catalog = price_catalog_from_snapshot(
                            game_config
                        )
                    except ValueError:
                        raise PawnhouseRepositoryError(
                            "invalid_frozen_price_catalog"
                        ) from None
                    result = await self._open_next_round(
                        connection,
                        game_id=game_id,
                        round_index=next_round,
                        base_prices=frozen_price_catalog.prices,
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
                g.market_protocol,
                g.config_snapshot,
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
                participant.readiness,
                participant.joined_at,
                participant.ready_at,
                (official.agent_id IS NOT NULL) AS is_official
            FROM arena402.game_participants AS participant
            LEFT JOIN public.arena_agents AS agent
              ON agent.agent_id = participant.agent_id
            LEFT JOIN arena402.official_agent_pool AS official
              ON official.agent_id = participant.agent_id
            WHERE participant.game_id = $1
              AND participant.status <> 'cancelled'
              AND participant.readiness <> 'withdrawn'
            ORDER BY participant.joined_at, participant.game_participant_id
            """,
            game["game_id"],
        )
        owner_participant_id: str | None = None
        if owner_user_id is not None:
            raw_owner_participant_id = await pool.fetchval(
                    """
                    SELECT game_participant_id
                    FROM arena402.game_participants
                    WHERE game_id = $1
                      AND user_id = $2
                      AND status <> 'cancelled'
                      AND readiness <> 'withdrawn'
                    """,
                    game["game_id"],
                    owner_user_id,
                )
            owner_participant_id = (
                None
                if raw_owner_participant_id is None
                else str(raw_owner_participant_id)
            )
        joined_by_me = owner_participant_id is not None

        phase = str(game["phase"])
        if phase in {"registration", "portfolio_setup", "portfolio_locked"}:
            status = "WAITING"
        elif phase in {"running", "final_valuation"}:
            status = "RUNNING"
        elif phase == "completed":
            status = "COMPLETED"
        else:
            raise PawnhouseRepositoryError("current_game_not_found")

        public_participants = [
            {
                "participantId": str(row["game_participant_id"]),
                "agentId": str(row["agent_id"]),
                "displayName": str(row["display_name"]),
                "runtimeKind": str(row["runtime_kind"]),
                "readiness": str(row["readiness"]).upper(),
                "joinedAt": row["joined_at"].isoformat(),
                "isOfficial": bool(row["is_official"]),
            }
            for row in participants
        ]
        config = (
            json.loads(game["config_snapshot"])
            if isinstance(game["config_snapshot"], str)
            else dict(game["config_snapshot"])
        )
        official_fill_after_seconds = int(
            config.get("officialFillAfterSeconds", 300)
        )
        first_human_ready_at = min(
            (
                row["ready_at"]
                for row in participants
                if not bool(row["is_official"])
                and str(row["readiness"]) == "ready"
                and row["ready_at"] is not None
            ),
            default=None,
        )
        fill_at = (
            first_human_ready_at
            + timedelta(seconds=official_fill_after_seconds)
            if first_human_ready_at is not None
            else None
        )
        server_time = datetime.now(timezone.utc)
        ready_count = sum(
            1
            for participant in public_participants
            if participant["readiness"] == "READY"
        )
        participating_count = len(public_participants)
        human_ready_count = sum(
            1
            for participant in public_participants
            if participant["readiness"] == "READY"
            and not participant["isOfficial"]
        )
        official_ready_count = ready_count - human_ready_count
        if status != "WAITING" or ready_count >= int(game["start_threshold"]):
            fill_status = "READY"
        elif participating_count >= int(game["start_threshold"]):
            fill_status = "PROVISIONING"
        elif first_human_ready_at is None:
            fill_status = "IDLE"
        elif fill_at is not None and server_time < fill_at:
            fill_status = "COLLECTING"
        else:
            has_eligible_official = bool(
                await pool.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM arena402.official_agent_pool AS official
                        JOIN public.arena_agents AS agent
                          ON agent.agent_id = official.agent_id
                         AND agent.status = 'active'
                        JOIN public.arena_runtime_bindings AS binding
                          ON binding.agent_id = official.agent_id
                         AND binding.runtime_kind = 'hosted'
                         AND binding.route_status = 'ready'
                         AND binding.disabled_at IS NULL
                        JOIN public.arena_hosted_configs AS hosted
                          ON hosted.hosted_config_id = binding.hosted_config_id
                         AND hosted.agent_id = official.agent_id
                         AND hosted.status = 'ready'
                        JOIN public.arena_model_credentials AS credential
                          ON credential.credential_id = hosted.credential_id
                         AND credential.status = 'valid'
                        JOIN arena402.wallet_inventory AS wallet
                          ON wallet.wallet_id = official.wallet_id
                         AND wallet.status <> 'disabled'
                        WHERE official.enabled
                          AND NOT EXISTS (
                              SELECT 1
                              FROM arena402.game_participants AS participant
                              WHERE participant.game_id = $1
                                AND participant.agent_id = official.agent_id
                                AND participant.readiness <> 'withdrawn'
                          )
                    )
                    """,
                    game["game_id"],
                )
            )
            fill_status = "FILLING" if has_eligible_official else "BLOCKED"
        return {
            "game": {
                "gameId": str(game["game_id"]),
                "status": status,
                "readyCount": ready_count,
                "startThreshold": int(game["start_threshold"]),
                "maxParticipants": int(game["max_participants"]),
                "roundCount": int(game["round_count"]),
                "currentRound": int(game["current_round"]),
                "marketProtocol": str(game["market_protocol"]),
                "roundPhase": (
                    str(game["round_phase"])
                    if game["round_phase"] is not None
                    else None
                ),
                "joinedByMe": joined_by_me,
                "myParticipantId": owner_participant_id,
                "participants": public_participants,
                "matchmaking": {
                    "targetSeats": int(game["start_threshold"]),
                    "humanReadyCount": human_ready_count,
                    "officialReadyCount": official_ready_count,
                    "firstHumanReadyAt": (
                        first_human_ready_at.isoformat()
                        if first_human_ready_at is not None
                        else None
                    ),
                    "fillAt": (
                        fill_at.isoformat() if fill_at is not None else None
                    ),
                    "fillStatus": fill_status,
                    "serverTime": server_time.isoformat(),
                },
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

    async def official_fill_plan(
        self,
        *,
        now: datetime,
    ) -> dict[str, object]:
        """Select allow-listed fillers after the first human's wait expires."""

        pool = self._require_pool()
        game = await pool.fetchrow(
            """
            SELECT g.game_id, g.phase, g.config_snapshot,
                   pointer.start_threshold
            FROM arena402.current_game AS pointer
            JOIN arena402.games AS g ON g.game_id = pointer.game_id
            WHERE pointer.singleton
            """
        )
        if game is None or str(game["phase"]) not in {
            "registration",
            "portfolio_setup",
            "portfolio_locked",
        }:
            return {
                "gameId": None if game is None else str(game["game_id"]),
                "status": "IDLE",
                "candidateAgentIds": [],
            }

        counts = await pool.fetchrow(
            """
            SELECT
                count(*) AS participating_count,
                count(*) FILTER (
                    WHERE participant.readiness = 'ready'
                ) AS ready_count,
                min(participant.ready_at) FILTER (
                    WHERE participant.readiness = 'ready'
                      AND official.agent_id IS NULL
                ) AS first_human_ready_at
            FROM arena402.game_participants AS participant
            LEFT JOIN arena402.official_agent_pool AS official
              ON official.agent_id = participant.agent_id
            WHERE participant.game_id = $1
              AND participant.status <> 'cancelled'
              AND participant.readiness <> 'withdrawn'
            """,
            game["game_id"],
        )
        ready_count = int(counts["ready_count"])
        participating_count = int(counts["participating_count"])
        target_seats = int(game["start_threshold"])
        first_human_ready_at = counts["first_human_ready_at"]
        if ready_count >= target_seats:
            return {
                "gameId": str(game["game_id"]),
                "status": "READY",
                "candidateAgentIds": [],
            }
        if participating_count >= target_seats:
            return {
                "gameId": str(game["game_id"]),
                "status": "PROVISIONING",
                "candidateAgentIds": [],
                "missingReadySeats": target_seats - ready_count,
            }
        if first_human_ready_at is None:
            return {
                "gameId": str(game["game_id"]),
                "status": "IDLE",
                "candidateAgentIds": [],
            }

        config = (
            json.loads(game["config_snapshot"])
            if isinstance(game["config_snapshot"], str)
            else dict(game["config_snapshot"])
        )
        fill_at = first_human_ready_at + timedelta(
            seconds=int(config.get("officialFillAfterSeconds", 300))
        )
        if now < fill_at:
            return {
                "gameId": str(game["game_id"]),
                "status": "COLLECTING",
                "fillAt": fill_at.isoformat(),
                "candidateAgentIds": [],
            }

        deficit = official_seat_deficit(
            target_seats=target_seats,
            ready_count=ready_count,
            participating_count=participating_count,
        )
        candidates = await pool.fetch(
            """
            SELECT official.agent_id
            FROM arena402.official_agent_pool AS official
            JOIN public.arena_agents AS agent
              ON agent.agent_id = official.agent_id
             AND agent.status = 'active'
            JOIN public.arena_runtime_bindings AS binding
              ON binding.agent_id = official.agent_id
             AND binding.runtime_kind = 'hosted'
             AND binding.route_status = 'ready'
             AND binding.disabled_at IS NULL
            JOIN public.arena_hosted_configs AS hosted
              ON hosted.hosted_config_id = binding.hosted_config_id
             AND hosted.agent_id = official.agent_id
             AND hosted.status = 'ready'
            JOIN public.arena_model_credentials AS credential
              ON credential.credential_id = hosted.credential_id
             AND credential.status = 'valid'
            JOIN arena402.wallet_inventory AS wallet
              ON wallet.wallet_id = official.wallet_id
             AND wallet.status <> 'disabled'
            WHERE official.enabled
              AND NOT EXISTS (
                  SELECT 1
                  FROM arena402.game_participants AS participant
                  WHERE participant.game_id = $1
                    AND participant.agent_id = official.agent_id
                    AND participant.readiness <> 'withdrawn'
              )
            ORDER BY
                md5(
                    $1::text || ':' || official.agent_id
                    || ':arena.official-selection.v1'
                ),
                official.priority,
                official.agent_id
            LIMIT $2
            """,
            game["game_id"],
            deficit,
        )
        candidate_agent_ids = [str(row["agent_id"]) for row in candidates]
        return {
            "gameId": str(game["game_id"]),
            "status": "FILLING" if candidate_agent_ids else "BLOCKED",
            "fillAt": fill_at.isoformat(),
            "candidateAgentIds": candidate_agent_ids,
            "missingSeats": deficit,
        }

    async def current_game_join_preflight(
        self,
        *,
        game_id: str,
        user_id: str,
        agent_id: str,
        key_digest: str,
        request_digest: str,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Issue a short-lived, non-reserving authorization for product Join."""

        issued_at = now or datetime.now(timezone.utc)
        join_authorization_expires_at = issued_at + timedelta(minutes=10)
        mandate_expires_at = issued_at + timedelta(hours=24)
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.fetchval(
                    """
                    SELECT pg_advisory_xact_lock(hashtext($1))
                    """,
                    f"arena402.join-preflight:{user_id}:{game_id}",
                )
                game = await connection.fetchrow(
                    """
                    SELECT g.game_id, g.phase, g.config_snapshot,
                           g.max_participants
                    FROM arena402.current_game AS pointer
                    JOIN arena402.games AS g ON g.game_id = pointer.game_id
                    WHERE pointer.singleton = TRUE
                      AND g.game_id = $1
                    FOR SHARE OF pointer, g
                    """,
                    game_id,
                )
                if game is None:
                    raise PawnhouseRepositoryError("game_not_current")
                if game["phase"] not in {"registration", "portfolio_setup"}:
                    raise PawnhouseRepositoryError("game_already_started")

                existing_participant = await connection.fetchrow(
                    """
                    SELECT game_participant_id
                    FROM arena402.game_participants
                    WHERE game_id = $1 AND user_id = $2
                    FOR SHARE
                    """,
                    game_id,
                    user_id,
                )
                if existing_participant is not None:
                    raise PawnhouseRepositoryError("user_already_joined")
                participant_count = int(
                    await connection.fetchval(
                        """
                        SELECT count(*)
                        FROM arena402.game_participants
                        WHERE game_id = $1
                          AND readiness <> 'withdrawn'
                        """,
                        game_id,
                    )
                )
                if participant_count >= int(game["max_participants"]):
                    raise PawnhouseRepositoryError(
                        "game_participant_limit_reached"
                    )

                runtime = await connection.fetchrow(
                    """
                    SELECT agent.agent_id
                    FROM public.arena_agents AS agent
                    WHERE agent.agent_id = $1
                      AND agent.owner_user_id = $2
                      AND agent.status = 'active'
                      AND (
                        EXISTS (
                            SELECT 1
                            FROM public.arena_runtime_bindings AS binding
                            JOIN public.arena_hosted_configs AS hosted
                              ON hosted.hosted_config_id =
                                 binding.hosted_config_id
                             AND hosted.agent_id = agent.agent_id
                             AND hosted.status = 'ready'
                            JOIN public.arena_model_credentials AS credential
                              ON credential.credential_id =
                                 hosted.credential_id
                             AND credential.status = 'valid'
                            WHERE binding.agent_id = agent.agent_id
                              AND binding.runtime_kind = 'hosted'
                              AND binding.disabled_at IS NULL
                              AND binding.route_status = 'ready'
                        )
                        OR EXISTS (
                            SELECT 1
                            FROM public.arena_runtime_bindings AS binding
                            JOIN LATERAL
                                resolve_connector_binding_for_arena(
                                    agent.owner_user_id,
                                    binding.connector_binding_id
                                ) AS route
                              ON route.binding_epoch =
                                 binding.connector_binding_epoch
                            WHERE binding.agent_id = agent.agent_id
                              AND binding.runtime_kind = 'connector'
                              AND binding.disabled_at IS NULL
                              AND binding.route_status = 'ready'
                        )
                      )
                    """,
                    agent_id,
                    user_id,
                )
                if runtime is None:
                    raise PawnhouseRepositoryError("runtime_not_ready")

                config = (
                    json.loads(game["config_snapshot"])
                    if isinstance(game["config_snapshot"], str)
                    else dict(game["config_snapshot"])
                )
                try:
                    frozen_price_catalog = price_catalog_from_snapshot(config)
                except ValueError:
                    raise PawnhouseRepositoryError(
                        "invalid_frozen_price_catalog"
                    ) from None
                settlement = self._settlement_config(config)
                if settlement.authorization_mode != "single_eip3009":
                    raise PawnhouseRepositoryError("settlement_not_available")
                wallet = await connection.fetchrow(
                    """
                    SELECT wallet_id, chain_id
                    FROM arena402.user_wallets
                    WHERE user_id = $1
                    FOR SHARE
                    """,
                    user_id,
                )
                if wallet is None or int(wallet["chain_id"]) != settlement.chain_id:
                    raise PawnhouseRepositoryError("wallet_not_ready")

                existing = await connection.fetchrow(
                    """
                    SELECT join_authorization_id, agent_id, request_digest,
                           expires_at, status
                    FROM arena402.join_authorizations
                    WHERE user_id = $1 AND key_digest = $2
                    FOR UPDATE
                    """,
                    user_id,
                    key_digest,
                )
                if existing is not None:
                    if (
                        existing["request_digest"] != request_digest
                        or existing["agent_id"] != agent_id
                    ):
                        raise PawnhouseRepositoryError("idempotency_conflict")
                    if (
                        existing["status"] == "pending"
                        and existing["expires_at"] > issued_at
                    ):
                        authorization_id = str(existing["join_authorization_id"])
                    else:
                        raise PawnhouseRepositoryError("join_authorization_expired")
                else:
                    await connection.execute(
                        """
                        UPDATE arena402.join_authorizations
                        SET status = 'expired'
                        WHERE user_id = $1
                          AND game_id = $2
                          AND status = 'pending'
                        """,
                        user_id,
                        game_id,
                    )
                    authorization_id = f"ja:{uuid.uuid4().hex}"
                    await connection.execute(
                        """
                        INSERT INTO arena402.join_authorizations (
                            join_authorization_id, user_id, game_id, agent_id,
                            key_digest, request_digest, expires_at
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        authorization_id,
                        user_id,
                        game_id,
                        agent_id,
                        key_digest,
                        request_digest,
                        join_authorization_expires_at,
                    )

        assert settlement.chain_id is not None
        assert settlement.token_address is not None
        default_portfolio = default_join_portfolio(
            game_id=game_id,
            agent_id=agent_id,
            prices=frozen_price_catalog.prices,
        )
        return {
            "gameId": game_id,
            "agentId": agent_id,
            "eligible": True,
            "readyToJoin": True,
            "joinAuthorizationId": authorization_id,
            "joinAuthorizationExpiresAt": (
                join_authorization_expires_at.isoformat()
            ),
            "checks": {
                "game": "READY",
                "agent": "READY",
                "runtime": "READY",
                "wallet": "READY",
                "paymentMandate": "ACTION_REQUIRED",
            },
            "mandateRequirements": {
                "chainId": settlement.chain_id,
                "tokenAddress": settlement.token_address,
                "tokenSymbol": settlement.token_symbol,
                "tokenDecimals": settlement.token_decimals,
                "maxPerPaymentAtomic": "10000000",
                "maxCumulativeAtomic": "50000000",
                "allowedPayeeRule": "SAME_GAME_SETTLEMENT_ACCOUNT",
                "expiresAt": mandate_expires_at.isoformat(),
            },
            "portfolioRequirements": {
                "initialNetWorthAtomic": str(INITIAL_NET_WORTH_ATOMIC),
                "goldDecimals": 6,
                "initialPricesAtomic": {
                    good_id: str(frozen_price_catalog.prices[good_id])
                    for good_id in GOOD_IDS
                },
                "allowedGoods": list(GOOD_IDS),
                "defaultPortfolio": {
                    "cashAtomic": str(default_portfolio.cash_atomic),
                    "holdings": default_portfolio.holdings,
                },
            },
            "safeErrorCode": None,
            "schemaVersion": "arena.game-join-preflight.v1",
        }

    async def game_owner_state(
        self,
        *,
        game_id: str,
        owner_user_id: str,
    ) -> dict[str, object]:
        """Return one authenticated participant's Arena-owned portfolio view."""

        pool = self._require_pool()
        participant = await pool.fetchrow(
            """
            SELECT
                participant.game_participant_id,
                participant.agent_id,
                coalesce(
                    game_agent.config_snapshot ->> 'display_name',
                    agent.name,
                    participant.agent_id
                ) AS display_name,
                participant.status,
                game.phase AS game_phase,
                balance.initial_cash_atomic,
                balance.cash_atomic,
                ranking.rank,
                ranking.net_worth_atomic,
                ranking.tier,
                ranking.calculated_at
            FROM arena402.game_participants AS participant
            JOIN arena402.games AS game
              ON game.game_id = participant.game_id
            JOIN arena402.balances AS balance
              ON balance.game_participant_id =
                 participant.game_participant_id
            LEFT JOIN public.game_agents AS game_agent
              ON game_agent.game_agent_id =
                 participant.game_participant_id
            LEFT JOIN public.arena_agents AS agent
              ON agent.agent_id = participant.agent_id
            LEFT JOIN arena402.rankings AS ranking
              ON ranking.game_id = participant.game_id
             AND ranking.game_participant_id =
                 participant.game_participant_id
            WHERE participant.game_id = $1
              AND participant.user_id = $2
              AND participant.status <> 'cancelled'
              AND participant.readiness <> 'withdrawn'
            """,
            game_id,
            owner_user_id,
        )
        if participant is None:
            raise PawnhouseRepositoryError("game_participant_not_found")
        participant_id = str(participant["game_participant_id"])
        holding_rows = await pool.fetch(
            """
            SELECT good_id, initial_quantity, quantity
            FROM arena402.holdings
            WHERE game_participant_id = $1
            ORDER BY good_id
            """,
            participant_id,
        )
        snapshot_rows = await pool.fetch(
            """
            SELECT
                round_id,
                round_index,
                cash_atomic,
                holdings_snapshot,
                captured_at
            FROM arena402.round_portfolio_snapshots
            WHERE game_id = $1
              AND game_participant_id = $2
            ORDER BY round_index
            """,
            game_id,
            participant_id,
        )
        reputation = await pool.fetchrow(
            """
            SELECT
                count(*) AS trade_attempts,
                count(*) FILTER (
                    WHERE status = 'settled'
                ) AS settled_trades,
                count(*) FILTER (
                    WHERE status IN ('rejected', 'timeout')
                ) AS failed_negotiations
            FROM arena402.pairings
            WHERE game_id = $1
              AND (
                  buyer_participant_id = $2
                  OR seller_participant_id = $2
              )
            """,
            game_id,
            participant_id,
        )
        assert reputation is not None
        trade_attempts = int(reputation["trade_attempts"])
        settled_trades = int(reputation["settled_trades"])
        initial_portfolio = {
            "cashAtomic": str(int(participant["initial_cash_atomic"])),
            "holdings": {
                str(row["good_id"]): int(row["initial_quantity"])
                for row in holding_rows
            },
        }
        current_portfolio = {
            "cashAtomic": str(int(participant["cash_atomic"])),
            "holdings": {
                str(row["good_id"]): int(row["quantity"])
                for row in holding_rows
            },
        }
        ranking = (
            None
            if participant["rank"] is None
            else {
                "rank": int(participant["rank"]),
                "netWorthAtomic": str(
                    int(participant["net_worth_atomic"])
                ),
                "tier": str(participant["tier"]),
                "calculatedAt": participant[
                    "calculated_at"
                ].isoformat(),
            }
        )
        return {
            "gameId": game_id,
            "participantId": participant_id,
            "agentId": str(participant["agent_id"]),
            "displayName": str(participant["display_name"]),
            "gamePhase": str(participant["game_phase"]),
            "participantStatus": str(participant["status"]),
            "initialPortfolio": initial_portfolio,
            "currentPortfolio": current_portfolio,
            "finalPortfolio": (
                current_portfolio
                if str(participant["game_phase"]) == "completed"
                else None
            ),
            "roundPortfolios": [
                {
                    "roundId": str(row["round_id"]),
                    "roundIndex": int(row["round_index"]),
                    "cashAtomic": str(int(row["cash_atomic"])),
                    "holdings": (
                        json.loads(row["holdings_snapshot"])
                        if isinstance(row["holdings_snapshot"], str)
                        else dict(row["holdings_snapshot"])
                    ),
                    "capturedAt": row["captured_at"].isoformat(),
                }
                for row in snapshot_rows
            ],
            "reputation": {
                "tradeAttempts": trade_attempts,
                "settledTrades": settled_trades,
                "successRateBps": (
                    None
                    if trade_attempts == 0
                    else settled_trades * 10_000 // trade_attempts
                ),
                "failedNegotiations": int(
                    reputation["failed_negotiations"]
                ),
            },
            "ranking": ranking,
            "schemaVersion": "arena.game-owner-state.v1",
        }

    async def game_state(self, game_id: str) -> dict[str, object]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            game = await connection.fetchrow(
                """
                SELECT
                    game_id, phase, round_count, current_round,
                    market_protocol,
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
                    participant.game_participant_id,
                    participant.agent_id,
                    coalesce(
                        game_agent.config_snapshot ->> 'display_name',
                        agent.name,
                        participant.agent_id
                    ) AS display_name,
                    participant.runtime_kind,
                    participant.status
                FROM arena402.game_participants AS participant
                LEFT JOIN public.game_agents AS game_agent
                  ON game_agent.game_agent_id =
                     participant.game_participant_id
                LEFT JOIN public.arena_agents AS agent
                  ON agent.agent_id = participant.agent_id
                WHERE participant.game_id = $1
                ORDER BY
                    participant.joined_at,
                    participant.game_participant_id
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
            pairings = await connection.fetch(
                """
                SELECT
                    pairing_id, round_id, good_id,
                    buyer_participant_id, seller_participant_id,
                    pairing_sequence, quantity, status,
                    paired_at, completed_at
                FROM arena402.pairings
                WHERE game_id = $1
                ORDER BY round_id, pairing_sequence, pairing_id
                """,
                game_id,
            )
            negotiations = await connection.fetch(
                """
                SELECT
                    negotiation_id, pairing_id, round_id,
                    max_turns, turn_count, next_role, status,
                    latest_proposal_price_atomic,
                    latest_proposal_role,
                    accepted_price_atomic,
                    action_deadline_at, created_at, completed_at
                FROM arena402.negotiations
                WHERE game_id = $1
                ORDER BY created_at, negotiation_id
                """,
                game_id,
            )
            rankings = await connection.fetch(
                """
                SELECT
                    r.rank, r.game_participant_id, p.agent_id,
                    coalesce(
                        game_agent.config_snapshot ->> 'display_name',
                        agent.name,
                        p.agent_id
                    ) AS display_name,
                    r.net_worth_atomic, r.tier, r.calculated_at
                FROM arena402.rankings AS r
                JOIN arena402.game_participants AS p
                  ON p.game_participant_id = r.game_participant_id
                LEFT JOIN public.game_agents AS game_agent
                  ON game_agent.game_agent_id = p.game_participant_id
                LEFT JOIN public.arena_agents AS agent
                  ON agent.agent_id = p.agent_id
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
            price_snapshots = await connection.fetch(
                """
                SELECT
                    snapshot.round_index,
                    snapshot.good_id,
                    snapshot.market_price_atomic,
                    coalesce(
                        (
                            SELECT previous.market_price_atomic
                            FROM arena402.price_snapshots AS previous
                            WHERE previous.game_id = snapshot.game_id
                              AND previous.good_id = snapshot.good_id
                              AND previous.round_index < snapshot.round_index
                            ORDER BY previous.round_index DESC
                            LIMIT 1
                        ),
                        game_good.initial_price_atomic
                    ) AS previous_market_price_atomic,
                    snapshot.final_price_atomic,
                    snapshot.supply_index_bps,
                    snapshot.bubble_premium_bps,
                    snapshot.created_at,
                    count(intent.settlement_intent_id) FILTER (
                        WHERE intent.status = 'inventory_committed'
                    ) AS committed_trade_count,
                    (
                        array_agg(
                            intent.unit_price_atomic
                            ORDER BY
                                intent.completed_at DESC,
                                intent.settlement_intent_id DESC
                        ) FILTER (
                            WHERE intent.status = 'inventory_committed'
                        )
                    )[1] AS last_clearing_price_atomic
                FROM arena402.price_snapshots AS snapshot
                JOIN arena402.game_goods AS game_good
                  ON game_good.game_id = snapshot.game_id
                 AND game_good.good_id = snapshot.good_id
                LEFT JOIN arena402.rounds AS round
                  ON round.game_id = snapshot.game_id
                 AND round.round_index = snapshot.round_index
                LEFT JOIN arena402.settlement_intents AS intent
                  ON intent.game_id = snapshot.game_id
                 AND intent.round_id = round.round_id
                 AND intent.good_id = snapshot.good_id
                WHERE snapshot.game_id = $1
                GROUP BY
                    snapshot.game_id,
                    snapshot.round_index,
                    snapshot.good_id,
                    snapshot.market_price_atomic,
                    game_good.initial_price_atomic,
                    snapshot.final_price_atomic,
                    snapshot.supply_index_bps,
                    snapshot.bubble_premium_bps,
                    snapshot.created_at
                ORDER BY snapshot.round_index, snapshot.good_id
                """,
                game_id,
            )
            live_rankings = await connection.fetch(
                """
                WITH valued AS (
                    SELECT
                        participant.game_participant_id,
                        participant.agent_id,
                        coalesce(
                            game_agent.config_snapshot ->> 'display_name',
                            agent.name,
                            participant.agent_id
                        ) AS display_name,
                        balance.cash_atomic
                        + coalesce(
                            sum(
                                holding.quantity
                                * coalesce(
                                    latest_price.market_price_atomic,
                                    game_good.initial_price_atomic
                                )
                            ),
                            0
                        ) AS net_worth_atomic
                    FROM arena402.game_participants AS participant
                    JOIN arena402.balances AS balance
                      ON balance.game_participant_id =
                         participant.game_participant_id
                    JOIN arena402.holdings AS holding
                      ON holding.game_participant_id =
                         participant.game_participant_id
                     AND holding.game_id = participant.game_id
                    JOIN arena402.game_goods AS game_good
                      ON game_good.game_id = holding.game_id
                     AND game_good.good_id = holding.good_id
                    LEFT JOIN LATERAL (
                        SELECT snapshot.market_price_atomic
                        FROM arena402.price_snapshots AS snapshot
                        WHERE snapshot.game_id = participant.game_id
                          AND snapshot.good_id = holding.good_id
                          AND snapshot.round_index <= $2
                        ORDER BY snapshot.round_index DESC
                        LIMIT 1
                    ) AS latest_price ON TRUE
                    LEFT JOIN public.game_agents AS game_agent
                      ON game_agent.game_agent_id =
                         participant.game_participant_id
                    LEFT JOIN public.arena_agents AS agent
                      ON agent.agent_id = participant.agent_id
                    WHERE participant.game_id = $1
                      AND participant.status <> 'cancelled'
                    GROUP BY
                        participant.game_participant_id,
                        participant.agent_id,
                        coalesce(
                            game_agent.config_snapshot ->> 'display_name',
                            agent.name,
                            participant.agent_id
                        ),
                        balance.cash_atomic
                )
                SELECT
                    row_number() OVER (
                        ORDER BY
                            net_worth_atomic DESC,
                            game_participant_id
                    ) AS rank,
                    game_participant_id,
                    agent_id,
                    display_name,
                    net_worth_atomic
                FROM valued
                ORDER BY rank
                """,
                game_id,
                int(game["current_round"]),
            )
            settlement_rows = await connection.fetch(
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
        return {
            "gameId": game["game_id"],
            "phase": game["phase"],
            "roundCount": game["round_count"],
            "currentRound": game["current_round"],
            "marketProtocol": str(game["market_protocol"]),
            "eventScheduleCommitment": game["event_schedule_commitment"],
            "eventSeed": (
                str(game["event_seed"])
                if game["event_seed_revealed_at"] is not None
                else None
            ),
            "participants": [
                {
                    "gameParticipantId": str(row["game_participant_id"]),
                    "agentId": str(row["agent_id"]),
                    "displayName": str(row["display_name"]),
                    "runtimeKind": str(row["runtime_kind"]),
                    "status": str(row["status"]),
                }
                for row in participants
            ],
            "rounds": [dict(row) for row in rounds],
            "pairings": [
                {
                    "pairingId": str(row["pairing_id"]),
                    "roundId": str(row["round_id"]),
                    "good": str(row["good_id"]),
                    "buyerParticipantId": str(row["buyer_participant_id"]),
                    "sellerParticipantId": str(row["seller_participant_id"]),
                    "sequence": int(row["pairing_sequence"]),
                    "quantity": int(row["quantity"]),
                    "status": str(row["status"]),
                    "pairedAt": row["paired_at"].isoformat(),
                    "completedAt": (
                        row["completed_at"].isoformat()
                        if row["completed_at"] is not None
                        else None
                    ),
                }
                for row in pairings
            ],
            "negotiations": [
                {
                    "negotiationId": str(row["negotiation_id"]),
                    "pairingId": str(row["pairing_id"]),
                    "roundId": str(row["round_id"]),
                    "maxTurns": int(row["max_turns"]),
                    "turnCount": int(row["turn_count"]),
                    "nextRole": str(row["next_role"]),
                    "status": str(row["status"]),
                    "latestProposalPriceAtomic": (
                        str(int(row["latest_proposal_price_atomic"]))
                        if row["latest_proposal_price_atomic"] is not None
                        else None
                    ),
                    "latestProposalRole": (
                        str(row["latest_proposal_role"])
                        if row["latest_proposal_role"] is not None
                        else None
                    ),
                    "acceptedPriceAtomic": (
                        str(int(row["accepted_price_atomic"]))
                        if row["accepted_price_atomic"] is not None
                        else None
                    ),
                    "actionDeadlineAt": row["action_deadline_at"].isoformat(),
                    "createdAt": row["created_at"].isoformat(),
                    "completedAt": (
                        row["completed_at"].isoformat()
                        if row["completed_at"] is not None
                        else None
                    ),
                }
                for row in negotiations
            ],
            "settlements": [
                self._settlement_public(row) for row in settlement_rows
            ],
            "priceSnapshots": [
                {
                    "roundIndex": int(row["round_index"]),
                    "goodId": str(row["good_id"]),
                    "marketPriceAtomic": str(
                        int(row["market_price_atomic"])
                    ),
                    "previousMarketPriceAtomic": str(
                        int(row["previous_market_price_atomic"])
                    ),
                    "eventImpliedFinalPriceAtomic": str(
                        int(row["final_price_atomic"])
                    ),
                    "supplyIndexBps": int(row["supply_index_bps"]),
                    "bubblePremiumBps": int(row["bubble_premium_bps"]),
                    "priceKind": "event_reference",
                    "committedTradeCount": int(
                        row["committed_trade_count"]
                    ),
                    "lastClearingAtomic": (
                        None
                        if row["last_clearing_price_atomic"] is None
                        else str(int(row["last_clearing_price_atomic"]))
                    ),
                    "createdAt": row["created_at"].isoformat(),
                }
                for row in price_snapshots
            ],
            "liveRankings": [
                {
                    "rank": int(row["rank"]),
                    "participantId": str(row["game_participant_id"]),
                    "agentId": str(row["agent_id"]),
                    "displayName": str(row["display_name"]),
                    "netWorthAtomic": str(int(row["net_worth_atomic"])),
                    "valuationPriceKind": "latest_event_reference",
                }
                for row in live_rankings
            ],
            "finalPrices": {
                str(row["good_id"]): str(int(row["price_atomic"]))
                for row in final_prices
            },
            "rankings": [
                {
                    "rank": int(row["rank"]),
                    "participantId": str(row["game_participant_id"]),
                    "agentId": str(row["agent_id"]),
                    "displayName": str(row["display_name"]),
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
                , quantity, limit_price_atomic
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
                quantity=int(row["quantity"]),
                limit_price_atomic=(
                    None
                    if row["limit_price_atomic"] is None
                    else int(row["limit_price_atomic"])
                ),
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
        if not pairings:
            return pairings

        pairing_rows = [
            {
                "pairing_id": pairing.pairing_id,
                "game_id": pairing.game_id,
                "round_id": pairing.round_id,
                "good_id": pairing.good,
                "buyer_entry_id": pairing.buyer_entry_id,
                "seller_entry_id": pairing.seller_entry_id,
                "buyer_participant_id": pairing.buyer_participant_id,
                "seller_participant_id": pairing.seller_participant_id,
                "pairing_sequence": pairing.sequence,
                "quantity": pairing.quantity,
                "buyer_limit_price_atomic": pairing.buyer_limit_price_atomic,
                "seller_limit_price_atomic": pairing.seller_limit_price_atomic,
            }
            for pairing in pairings
        ]
        await connection.execute(
            """
            INSERT INTO arena402.pairings (
                pairing_id, game_id, round_id, good_id,
                buyer_entry_id, seller_entry_id,
                buyer_participant_id, seller_participant_id,
                pairing_sequence, quantity,
                buyer_limit_price_atomic, seller_limit_price_atomic
            )
            SELECT
                item.pairing_id, item.game_id, item.round_id, item.good_id,
                item.buyer_entry_id, item.seller_entry_id,
                item.buyer_participant_id, item.seller_participant_id,
                item.pairing_sequence, item.quantity,
                item.buyer_limit_price_atomic,
                item.seller_limit_price_atomic
            FROM jsonb_to_recordset($1::jsonb) AS item(
                pairing_id text,
                game_id text,
                round_id text,
                good_id text,
                buyer_entry_id text,
                seller_entry_id text,
                buyer_participant_id text,
                seller_participant_id text,
                pairing_sequence integer,
                quantity integer,
                buyer_limit_price_atomic numeric,
                seller_limit_price_atomic numeric
            )
            """,
            _json(pairing_rows),
        )
        await connection.execute(
            """
            UPDATE arena402.pool_entries
            SET status = 'paired'
            WHERE pool_entry_id = ANY($1::text[])
            """,
            [
                entry_id
                for pairing in pairings
                for entry_id in (
                    pairing.buyer_entry_id,
                    pairing.seller_entry_id,
                )
            ],
        )
        await connection.execute(
            """
            INSERT INTO arena402.negotiations (
                negotiation_id, pairing_id, game_id, round_id,
                buyer_participant_id, seller_participant_id, max_turns,
                action_deadline_at
            )
            SELECT
                'neg:' || item.pairing_id,
                item.pairing_id,
                item.game_id,
                item.round_id,
                item.buyer_participant_id,
                item.seller_participant_id,
                game.max_negotiation_turns,
                clock_timestamp() + $2 * interval '1 millisecond'
            FROM jsonb_to_recordset($1::jsonb) AS item(
                pairing_id text,
                game_id text,
                round_id text,
                buyer_participant_id text,
                seller_participant_id text
            )
            JOIN arena402.games AS game
              ON game.game_id = item.game_id
            """,
            _json(pairing_rows),
            timeout_ms,
        )
        await connection.execute(
            """
            INSERT INTO arena402.game_events (
                game_id, round_id, event_type, public_payload,
                source_idempotency_key
            )
            SELECT
                $1,
                $2,
                'pairing.created',
                item.public_payload,
                item.source_key
            FROM jsonb_to_recordset($3::jsonb) AS item(
                source_key text,
                public_payload jsonb
            )
            ON CONFLICT (game_id, source_idempotency_key) DO NOTHING
            """,
            game_id,
            round_id,
            _json(
                [
                    {
                        "source_key": f"{pairing.pairing_id}:created",
                        "public_payload": self._pairing_public(pairing),
                    }
                    for pairing in pairings
                ]
            ),
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
            if negotiation.status in {
                NegotiationStatus.REJECTED,
                NegotiationStatus.TIMEOUT,
            }:
                await self._pairing_closed_event(
                    connection,
                    game_id=game_id,
                    round_id=round_id,
                    pairing_id=row["pairing_id"],
                    negotiation_id=negotiation.negotiation_id,
                    status=negotiation.status.value,
                )
            if (
                negotiation.status
                is NegotiationStatus.ACCEPTED_PENDING_SETTLEMENT
            ):
                intent = await self._freeze_settlement_intent(
                    connection,
                    negotiation_id=negotiation.negotiation_id,
                )
                if intent is None:
                    negotiation.status = NegotiationStatus.SETTLEMENT_FAILED
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
                p.quantity,
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
            completed_at = datetime.now(timezone.utc)
            await connection.execute(
                """
                UPDATE arena402.pairings
                SET status = 'settlement_failed',
                    completed_at = COALESCE(completed_at, $2)
                WHERE pairing_id = $1
                  AND status = 'accepted_pending_settlement'
                """,
                row["pairing_id"],
                completed_at,
            )
            await connection.execute(
                """
                UPDATE arena402.negotiations
                SET status = 'settlement_failed',
                    next_role = 'none',
                    completed_at = COALESCE(completed_at, $2)
                WHERE negotiation_id = $1
                  AND status = 'accepted_pending_settlement'
                """,
                negotiation_id,
                completed_at,
            )
            await self._pairing_closed_event(
                connection,
                game_id=str(row["game_id"]),
                round_id=str(row["round_id"]),
                pairing_id=str(row["pairing_id"]),
                negotiation_id=negotiation_id,
                status=NegotiationStatus.SETTLEMENT_FAILED.value,
                safe_error_code="settlement_disabled",
            )
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
        quantity = int(row["quantity"])
        amount = accepted_price * quantity
        if int(row["buyer_cash_atomic"]) < amount:
            raise PawnhouseRepositoryError(
                "buyer_has_insufficient_cash_for_settlement"
            )
        if int(row["seller_quantity"]) < quantity:
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
            quantity=quantity,
            unit_price_atomic=accepted_price,
            amount_atomic=amount,
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
                $23, $24, $25::jsonb, $26
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
    def _ledger_trade_public(
        row: Mapping[str, object],
    ) -> dict[str, object]:
        confirmed_at = (
            row.get("confirmation_observed_at")
            or row.get("chain_confirmed_at")
        )
        created_at = row["created_at"]
        return {
            "tradeId": str(row["settlement_intent_id"]),
            "gameId": str(row["game_id"]),
            "round": int(row["round_index"]),
            "goodId": str(row["good_id"]),
            "quantity": int(row["quantity"]),
            "priceAtomic": str(int(row["unit_price_atomic"])),
            "amountAtomic": str(int(row["amount_atomic"])),
            "buyer": {
                "agentId": str(row["buyer_agent_id"]),
                "displayName": str(row["buyer_display_name"]),
                "accountAddress": str(row["buyer_account"]),
            },
            "seller": {
                "agentId": str(row["seller_agent_id"]),
                "displayName": str(row["seller_display_name"]),
                "accountAddress": str(row["seller_account"]),
            },
            "pairingId": str(row["pairing_id"]),
            "chainId": int(row["chain_id"]),
            "txHash": (
                str(row["tx_hash"])
                if row.get("tx_hash") is not None
                else None
            ),
            "blockNumber": (
                str(int(row["block_number"]))
                if row.get("block_number") is not None
                else None
            ),
            "chainConfirmedAt": (
                confirmed_at.isoformat()
                if hasattr(confirmed_at, "isoformat")
                else confirmed_at
            ),
            "facilitatorAddress": row.get("facilitator_address"),
            "status": str(row["status"]),
            "createdAt": (
                created_at.isoformat()
                if hasattr(created_at, "isoformat")
                else str(created_at)
            ),
            "schemaVersion": "arena402.trade-ledger-entry.v1",
        }

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
              AND p.readiness = 'ready'
              AND p.status = 'active'
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
        base_prices: Mapping[GoodId, int] = INITIAL_PRICES,
    ) -> dict[str, object]:
        events = await self._scheduled_events(
            connection,
            game_id=game_id,
        )
        event_by_round = {event.reveal_round: event for event in events}
        event = event_by_round.get(round_index)
        if event is None:
            raise PawnhouseRepositoryError("round_event_not_found")
        world = WorldState(
            {value.event_id: value for value in events},
            base_prices=dict(base_prices),
        )
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

        last_clearing_prices, buy_pressure_bps = await self._market_feedback(
            connection,
            game_id=game_id,
            before_round_index=round_index,
        )
        snapshot = apply_market_feedback(
            snapshot,
            last_clearing_prices=last_clearing_prices,
            buy_pressure_bps=buy_pressure_bps,
        )

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

    @staticmethod
    async def _market_feedback(
        connection: Any,
        *,
        game_id: str,
        before_round_index: int,
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Read only settled history used to seed the next market snapshot."""

        trade_rows = await connection.fetch(
            """
            SELECT DISTINCT ON (i.good_id)
                i.good_id, i.unit_price_atomic
            FROM arena402.settlement_intents AS i
            JOIN arena402.rounds AS r
              ON r.round_id = i.round_id
             AND r.game_id = i.game_id
            WHERE i.game_id = $1
              AND i.status = 'inventory_committed'
              AND r.round_index < $2
            ORDER BY i.good_id, r.round_index DESC,
                     i.completed_at DESC, i.settlement_intent_id DESC
            """,
            game_id,
            before_round_index,
        )
        decision_rows = await connection.fetch(
            """
            SELECT
                e.public_payload->>'good' AS good_id,
                COUNT(*) FILTER (
                    WHERE e.public_payload->>'action' = 'buy'
                ) AS buy_count,
                COUNT(*) FILTER (
                    WHERE e.public_payload->>'action' = 'sell'
                ) AS sell_count
            FROM arena402.game_events AS e
            JOIN arena402.rounds AS r
              ON r.round_id = e.round_id
             AND r.game_id = e.game_id
            WHERE e.game_id = $1
              AND e.event_type = 'decision.applied'
              AND r.round_index < $2
            GROUP BY e.public_payload->>'good'
            """,
            game_id,
            before_round_index,
        )
        prices = {
            str(row["good_id"]): int(row["unit_price_atomic"])
            for row in trade_rows
        }
        pressure: dict[str, int] = {}
        for row in decision_rows:
            buy_count = int(row["buy_count"])
            sell_count = int(row["sell_count"])
            total = buy_count + sell_count
            pressure[str(row["good_id"])] = (
                0
                if total == 0
                else max(
                    -10_000,
                    min(10_000, ((buy_count - sell_count) * 10_000) // total),
                )
            )
        return prices, pressure

    async def _finalize_game(
        self,
        connection: Any,
        *,
        game_id: str,
        round_index: int,
        event_seed: str,
        event_schedule_commitment: str,
    ) -> list[dict[str, object]]:
        # Round close is the normal owner of market cleanup. The game-wide
        # pass also repairs any older round that reached completion before
        # that cleanup was introduced or was missed by a recovery path.
        await self._terminalize_agent_market_scope(
            connection,
            game_id=game_id,
            round_id=None,
        )
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
              AND p.readiness = 'ready'
              AND p.status = 'active'
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
              AND readiness = 'ready'
              AND status = 'active'
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
              AND game_agent_id IN (
                  SELECT game_participant_id
                  FROM arena402.game_participants
                  WHERE game_id = $1
                    AND readiness = 'ready'
              )
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

    @staticmethod
    async def _terminalize_agent_market_scope(
        connection: Any,
        *,
        game_id: str,
        round_id: str | None,
    ) -> None:
        """Close unfinished A2A market state at a durable round boundary."""

        await connection.execute(
            """
            UPDATE arena402.market_rfq_sessions
            SET status = 'expired',
                deadline_at = LEAST(
                    deadline_at,
                    clock_timestamp()
                ),
                updated_at = clock_timestamp()
            WHERE game_id = $1
              AND ($2::text IS NULL OR round_id = $2)
              AND status = 'active'
            """,
            game_id,
            round_id,
        )
        await connection.execute(
            """
            UPDATE arena402.market_negotiation_requests
            SET status = 'expired'
            WHERE game_id = $1
              AND ($2::text IS NULL OR round_id = $2)
              AND status = 'pending'
            """,
            game_id,
            round_id,
        )
        await connection.execute(
            """
            UPDATE arena402.participant_round_slots
            SET status = 'available',
                engagement_id = NULL,
                version = version + 1,
                updated_at = clock_timestamp()
            WHERE game_id = $1
              AND ($2::text IS NULL OR round_id = $2)
              AND status = 'reserved'
            """,
            game_id,
            round_id,
        )
        await connection.execute(
            """
            UPDATE arena402.market_intents
            SET status = 'expired',
                expires_at = LEAST(
                    expires_at,
                    clock_timestamp()
                )
            WHERE game_id = $1
              AND ($2::text IS NULL OR round_id = $2)
              AND status IN ('open', 'reserved')
            """,
            game_id,
            round_id,
        )

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

    async def _pairing_closed_event(
        self,
        connection: Any,
        *,
        game_id: str,
        round_id: str,
        pairing_id: str,
        negotiation_id: str,
        status: str,
        settlement_intent_id: str | None = None,
        safe_error_code: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "pairingId": pairing_id,
            "negotiationId": negotiation_id,
            "status": status,
        }
        if settlement_intent_id is not None:
            payload["settlementIntentId"] = settlement_intent_id
        if safe_error_code is not None:
            payload["safeErrorCode"] = safe_error_code
        if status in {"settled", "settlement_failed"}:
            await connection.execute(
                """
                UPDATE arena402.market_engagements
                SET status = $2,
                    completed_at = COALESCE(
                        completed_at,
                        clock_timestamp()
                    )
                WHERE negotiation_id = $1
                  AND status IN (
                      'accepted_pending_settlement',
                      'settling'
                  )
                """,
                negotiation_id,
                status,
            )
        await self._event(
            connection,
            game_id=game_id,
            round_id=round_id,
            event_type="pairing.closed",
            source_key=f"{pairing_id}:closed",
            public_payload=payload,
        )

    @staticmethod
    def _pairing_public(pairing: Pairing) -> dict[str, object]:
        return {
            "pairingId": pairing.pairing_id,
            "good": pairing.good,
            "buyerParticipantId": pairing.buyer_participant_id,
            "sellerParticipantId": pairing.seller_participant_id,
            "quantity": pairing.quantity,
            "buyerLimitPriceAtomic": pairing.buyer_limit_price_atomic,
            "sellerLimitPriceAtomic": pairing.seller_limit_price_atomic,
            "sequence": pairing.sequence,
        }


__all__ = [
    "PawnhouseRepositoryError",
    "PostgresPawnhouseRepository",
]
