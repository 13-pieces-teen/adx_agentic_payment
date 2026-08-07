from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from arena_game import build_event_schedule
from arena_game.postgres import (
    PawnhouseRepositoryError,
    PostgresPawnhouseRepository,
    _should_assign_balanced_portfolios,
)


class _Pool:
    def __init__(self, *, eligible_officials: int = 19) -> None:
        self.queries: list[str] = []
        self.eligible_officials = eligible_officials

    async def fetchrow(self, query: str, *_: object):
        self.queries.append(query)
        return {
            "game_id": "game-current",
            "phase": "portfolio_setup",
            "start_threshold": 20,
            "max_participants": 20,
            "round_count": 5,
            "current_round": 0,
            "market_protocol": "agent_a2a.v1",
            "config_snapshot": {"officialFillAfterSeconds": 300},
            "round_phase": None,
            "created_at": datetime(2026, 7, 25, 9, tzinfo=timezone.utc),
            "started_at": None,
            "completed_at": None,
        }

    async def fetch(self, query: str, *_: object):
        self.queries.append(query)
        return [
            {
                "game_participant_id": "gp:game-current:agent-1",
                "agent_id": "agent-1",
                "display_name": "Merchant Fox",
                "runtime_kind": "hosted",
                "readiness": "ready",
                "ready_at": datetime(
                    2026,
                    7,
                    25,
                    10,
                    tzinfo=timezone.utc,
                ),
                "is_official": False,
                "joined_at": datetime(
                    2026,
                    7,
                    25,
                    10,
                    tzinfo=timezone.utc,
                ),
            }
        ]

    async def fetchval(self, query: str, *_: object):
        self.queries.append(query)
        return self.eligible_officials


class _OwnerProjectionPool:
    async def fetchrow(self, query: str, *_: object):
        if "FROM arena402.game_participants AS participant" in query:
            return {
                "game_participant_id": "participant-owner",
                "agent_id": "agent-owner",
                "display_name": "Owner Agent",
                "status": "completed",
                "game_phase": "completed",
                "initial_cash_atomic": 20_000_000,
                "cash_atomic": 17_000_000,
                "rank": 2,
                "net_worth_atomic": 23_000_000,
                "tier": "御用商人",
                "calculated_at": datetime(
                    2026, 7, 27, 12, tzinfo=timezone.utc
                ),
            }
        if "FROM arena402.pairings" in query:
            return {
                "trade_attempts": 3,
                "settled_trades": 2,
                "failed_negotiations": 1,
            }
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query: str, *_: object):
        if "FROM arena402.holdings" in query:
            return [
                {
                    "good_id": "grain",
                    "initial_quantity": 0,
                    "quantity": 2,
                }
            ]
        if "FROM arena402.round_portfolio_snapshots" in query:
            return [
                {
                    "round_id": "round-1",
                    "round_index": 1,
                    "cash_atomic": 17_000_000,
                    "holdings_snapshot": {"grain": 2},
                    "captured_at": datetime(
                        2026, 7, 27, 11, tzinfo=timezone.utc
                    ),
                }
            ]
        raise AssertionError(f"Unexpected fetch query: {query}")


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Acquire:
    def __init__(self, connection: "_CreateConnection") -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _CreateConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.fetchval_calls: list[tuple[str, tuple[object, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self):
        return _Transaction()

    async def fetchval(
        self,
        query: str,
        game_id: str | None = None,
        *_: object,
    ):
        self.queries.append(query)
        self.fetchval_calls.append((query, (game_id, *_)))
        return game_id

    async def execute(self, query: str, *arguments: object):
        self.queries.append(query)
        self.execute_calls.append((query, arguments))
        return "OK"


class _ExistingCurrentConnection(_CreateConnection):
    async def fetchrow(self, query: str, *_: object):
        self.queries.append(query)
        return {"game_id": "game-current", "phase": "running"}


class _CompletedCurrentConnection(_CreateConnection):
    async def fetchrow(self, query: str, *_: object):
        self.queries.append(query)
        return {"game_id": "game-completed", "phase": "completed"}


class _CreatePool:
    def __init__(self, connection: _CreateConnection) -> None:
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


class _HistoricalGameStateConnection(_CreateConnection):
    async def fetchrow(self, query: str, *_: object):
        self.queries.append(query)
        if "FROM arena402.games" in query:
            return {
                "game_id": "game-completed",
                "phase": "completed",
                "round_count": 1,
                "current_round": 1,
                "market_protocol": "agent_a2a.v1",
                "event_schedule_commitment": "sha256:" + "0" * 64,
                "event_seed": "historical-seed",
                "event_seed_revealed_at": datetime(
                    2026, 7, 27, 12, tzinfo=timezone.utc
                ),
                "started_at": datetime(
                    2026, 7, 27, 11, tzinfo=timezone.utc
                ),
                "completed_at": datetime(
                    2026, 7, 27, 12, tzinfo=timezone.utc
                ),
            }
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query: str, *_: object):
        self.queries.append(query)
        if "WITH valued AS" in query:
            return [
                {
                    "rank": 1,
                    "game_participant_id": "gp:game-completed:agent-random",
                    "agent_id": "agent_random0123456789",
                    "display_name": "Player Merchant",
                    "net_worth_atomic": 20_000_000,
                }
            ]
        if "FROM arena402.game_participants" in query:
            return [
                {
                    "game_participant_id": "gp:game-completed:agent-random",
                    "agent_id": "agent_random0123456789",
                    "display_name": "Player Merchant",
                    "runtime_kind": "hosted",
                    "status": "completed",
                }
            ]
        if "FROM arena402.rounds" in query:
            return [
                {
                    "round_id": "round:game-completed:1",
                    "round_index": 1,
                    "phase": "closed",
                    "phase_deadline_at": None,
                }
            ]
        if "FROM arena402.pairings" in query:
            return [
                {
                    "pairing_id": "pair:game-completed:1",
                    "round_id": "round:game-completed:1",
                    "good_id": "gems",
                    "buyer_participant_id": "gp:game-completed:buyer",
                    "seller_participant_id": "gp:game-completed:seller",
                    "pairing_sequence": 1,
                    "quantity": 1,
                    "status": "settled",
                    "paired_at": datetime(
                        2026, 7, 27, 11, 30, tzinfo=timezone.utc
                    ),
                    "completed_at": datetime(
                        2026, 7, 27, 11, 31, tzinfo=timezone.utc
                    ),
                }
            ]
        if "FROM arena402.negotiations" in query:
            return [
                {
                    "negotiation_id": "neg:pair:game-completed:1",
                    "pairing_id": "pair:game-completed:1",
                    "round_id": "round:game-completed:1",
                    "max_turns": 3,
                    "turn_count": 3,
                    "next_role": "none",
                    "status": "settled",
                    "latest_proposal_price_atomic": 4_374_844,
                    "latest_proposal_role": "seller",
                    "accepted_price_atomic": 4_374_844,
                    "action_deadline_at": datetime(
                        2026, 7, 27, 11, 32, tzinfo=timezone.utc
                    ),
                    "created_at": datetime(
                        2026, 7, 27, 11, 30, tzinfo=timezone.utc
                    ),
                    "completed_at": datetime(
                        2026, 7, 27, 11, 31, tzinfo=timezone.utc
                    ),
                }
            ]
        if "FROM arena402.rankings" in query:
            return [
                {
                    "rank": 1,
                    "game_participant_id": (
                        "gp:game-completed:agent-random"
                    ),
                    "agent_id": "agent_random0123456789",
                    "display_name": "Player Merchant",
                    "net_worth_atomic": 20_000_000,
                    "tier": "公爵",
                    "calculated_at": datetime(
                        2026, 7, 27, 12, tzinfo=timezone.utc
                    ),
                }
            ]
        if "FROM arena402.final_settlement_prices" in query:
            return []
        if "FROM arena402.price_snapshots" in query:
            return [
                {
                    "round_index": 1,
                    "good_id": "gems",
                    "market_price_atomic": 4_000_000,
                    "previous_market_price_atomic": 3_000_000,
                    "final_price_atomic": 4_374_844,
                    "supply_index_bps": 10_000,
                    "bubble_premium_bps": 0,
                    "committed_trade_count": 1,
                    "last_clearing_price_atomic": 4_374_844,
                    "created_at": datetime(
                        2026, 7, 27, 11, 1, tzinfo=timezone.utc
                    ),
                }
            ]
        if "FROM arena402.settlement_intents AS i" in query:
            return []
        raise AssertionError(f"Unexpected fetch query: {query}")


class _HostedContextConnection(_CreateConnection):
    async def fetchrow(self, query: str, *_: object):
        self.queries.append(query)
        if "FROM arena402.rounds AS r" in query:
            return {
                "round_id": "round:game-current:1",
                "round_index": 1,
                "phase_deadline_at": datetime(
                    2026, 8, 8, 0, 30, tzinfo=timezone.utc
                ),
                "action_timeout_ms": 120_000,
                "round_count": 8,
            }
        if "market.liquidity_summarized" in query:
            return None
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query: str, *_: object):
        self.queries.append(query)
        return []


class _JoinPreflightConnection(_CreateConnection):
    participant_count = 0
    existing_participant = None

    async def fetchrow(self, query: str, *_: object):
        self.queries.append(query)
        if "FROM arena402.current_game" in query:
            return {
                "game_id": "game-current",
                "phase": "portfolio_setup",
                "max_participants": 100,
                "config_snapshot": {
                    "priceCatalogId": "pawnhouse-price-v2",
                    "initialPricesAtomic": {
                        "grain": "2500000",
                        "iron": "4500000",
                        "warhorse": "7000000",
                        "gems": "4000000",
                    },
                    "settlement": {
                        "authorizationMode": "single_eip3009",
                        "chainId": 1439,
                        "tokenAddress": "0x" + "11" * 20,
                        "tokenSymbol": "mUSDC",
                        "tokenDecimals": 6,
                        "tokenEip712Name": "Mock USD Coin",
                        "tokenEip712Version": "1",
                        "requiredConfirmations": 2,
                    }
                },
            }
        if "FROM arena402.game_participants" in query:
            return self.existing_participant
        if "FROM public.arena_agents AS agent" in query:
            return {"runtime_binding_id": "binding-ready"}
        if "FROM arena402.user_wallets" in query:
            return {"wallet_id": "wallet-ready", "chain_id": 1439}
        if "FROM arena402.join_authorizations" in query:
            return None
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetchval(self, query: str, *_: object):
        self.queries.append(query)
        if "pg_advisory_xact_lock" in query:
            return None
        if "count(*)" in query:
            return self.participant_count
        raise AssertionError(f"Unexpected fetchval query: {query}")


def test_current_game_uses_authoritative_pointer_and_safe_projection() -> None:
    pool = _Pool()
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=pool,
    )

    value = asyncio.run(repository.current_game(owner_user_id=None))

    assert value == {
        "game": {
            "gameId": "game-current",
            "status": "WAITING",
            "readyCount": 1,
            "startThreshold": 20,
            "maxParticipants": 20,
            "roundCount": 5,
            "currentRound": 0,
            "marketProtocol": "agent_a2a.v1",
            "roundPhase": None,
            "joinedByMe": False,
            "myParticipantId": None,
            "participants": [
                {
                    "participantId": "gp:game-current:agent-1",
                    "agentId": "agent-1",
                    "displayName": "Merchant Fox",
                    "runtimeKind": "hosted",
                    "readiness": "READY",
                    "joinedAt": "2026-07-25T10:00:00+00:00",
                    "isOfficial": False,
                }
            ],
            "matchmaking": {
                "targetSeats": 20,
                "humanReadyCount": 1,
                "officialReadyCount": 0,
                "firstHumanReadyAt": "2026-07-25T10:00:00+00:00",
                "fillAt": "2026-07-25T10:05:00+00:00",
                "fillStatus": "FILLING",
                "serverTime": value["game"]["matchmaking"]["serverTime"],
            },
            "createdAt": "2026-07-25T09:00:00+00:00",
            "startedAt": None,
            "completedAt": None,
        },
        "nextGamePending": False,
        "schemaVersion": "arena.current-game.v1",
    }
    assert "arena402.current_game" in pool.queries[0]
    assert all(
        "user_id" not in participant
        for participant in value["game"]["participants"]
    )


def test_historical_game_state_exposes_agent_identity_without_owner_data() -> None:
    connection = _HistoricalGameStateConnection()
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_CreatePool(connection),
    )

    value = asyncio.run(repository.game_state("game-completed"))

    assert value["participants"] == [
        {
            "gameParticipantId": "gp:game-completed:agent-random",
            "agentId": "agent_random0123456789",
            "displayName": "Player Merchant",
            "runtimeKind": "hosted",
            "status": "completed",
        }
    ]
    assert value["rankings"][0]["displayName"] == "Player Merchant"
    assert value["pairings"][0]["status"] == "settled"
    assert value["negotiations"][0]["status"] == "settled"
    assert value["negotiations"][0]["acceptedPriceAtomic"] == "4374844"
    assert value["marketProtocol"] == "agent_a2a.v1"
    assert value["priceSnapshots"][0]["priceKind"] == "event_reference"
    assert value["priceSnapshots"][0]["previousMarketPriceAtomic"] == "3000000"
    assert value["liveRankings"][0]["netWorthAtomic"] == "20000000"
    assert value["settlements"] == []
    assert "userId" not in json.dumps(value)
    live_rankings_query = next(
        query for query in connection.queries if "WITH valued AS" in query
    )
    assert (
        "GROUP BY\n"
        "                        participant.game_participant_id,\n"
        "                        participant.agent_id,\n"
        "                        coalesce("
    ) in live_rankings_query


def test_hosted_context_orders_liquidity_by_real_game_event_columns() -> None:
    connection = _HostedContextConnection()
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_CreatePool(connection),
    )

    value = asyncio.run(
        repository.hosted_decide_contexts(game_id="game-current")
    )

    assert value == []
    liquidity_query = next(
        query
        for query in connection.queries
        if "market.liquidity_summarized" in query
    )
    assert "event.created_at DESC" in liquidity_query
    assert "event.event_sequence DESC" in liquidity_query
    assert "event.occurred_at" not in liquidity_query
    assert "event.event_id" not in liquidity_query


def test_owner_game_state_projects_real_portfolios_and_reputation() -> None:
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_OwnerProjectionPool(),
    )

    value = asyncio.run(
        repository.game_owner_state(
            game_id="game-completed",
            owner_user_id="owner-user",
        )
    )

    assert value["initialPortfolio"] == {
        "cashAtomic": "20000000",
        "holdings": {"grain": 0},
    }
    assert value["finalPortfolio"] == {
        "cashAtomic": "17000000",
        "holdings": {"grain": 2},
    }
    assert value["reputation"] == {
        "tradeAttempts": 3,
        "settledTrades": 2,
        "successRateBps": 6666,
        "failedNegotiations": 1,
    }
    assert value["roundPortfolios"][0]["roundIndex"] == 1
    assert value["ranking"]["netWorthAtomic"] == "23000000"
    assert "owner-user" not in json.dumps(value)


def test_pairing_closed_event_has_a_stable_public_contract() -> None:
    connection = _CreateConnection()
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_CreatePool(connection),
    )

    asyncio.run(
        repository._pairing_closed_event(
            connection,
            game_id="game-1",
            round_id="round-1",
            pairing_id="pair-1",
            negotiation_id="neg:pair-1",
            status="settled",
            settlement_intent_id="settlement:neg:pair-1",
        )
    )

    query, arguments = connection.execute_calls[-1]
    assert "INSERT INTO arena402.game_events" in query
    assert arguments[0:4] == (
        "game-1",
        "round-1",
        "pairing.closed",
        (
            '{"negotiationId":"neg:pair-1","pairingId":"pair-1",'
            '"settlementIntentId":"settlement:neg:pair-1",'
            '"status":"settled"}'
        ),
    )
    assert arguments[4] == "pair-1:closed"


def test_pairing_closed_event_terminalizes_the_a2a_engagement() -> None:
    connection = _CreateConnection()
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_CreatePool(connection),
    )

    asyncio.run(
        repository._pairing_closed_event(
            connection,
            game_id="game-1",
            round_id="round-1",
            pairing_id="pair-1",
            negotiation_id="neg:pair-1",
            status="settlement_failed",
            settlement_intent_id="settlement:neg:pair-1",
        )
    )

    engagement_query, engagement_arguments = connection.execute_calls[-2]
    assert "UPDATE arena402.market_engagements" in engagement_query
    assert "status IN (" in engagement_query
    assert engagement_arguments == ("neg:pair-1", "settlement_failed")


def test_pairing_closed_event_can_explain_a_safe_settlement_failure() -> None:
    connection = _CreateConnection()
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_CreatePool(connection),
    )

    asyncio.run(
        repository._pairing_closed_event(
            connection,
            game_id="game-1",
            round_id="round-1",
            pairing_id="pair-1",
            negotiation_id="neg:pair-1",
            status="settlement_failed",
            safe_error_code="settlement_disabled",
        )
    )

    _, arguments = connection.execute_calls[-1]
    assert arguments[3] == (
        '{"negotiationId":"neg:pair-1","pairingId":"pair-1",'
        '"safeErrorCode":"settlement_disabled",'
        '"status":"settlement_failed"}'
    )


def test_current_game_reports_blocked_when_official_pool_cannot_fill_deficit() -> None:
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_Pool(eligible_officials=0),
    )

    value = asyncio.run(repository.current_game(owner_user_id=None))

    assert value["game"]["matchmaking"]["fillStatus"] == "BLOCKED"


def test_successful_join_preflight_explicitly_authorizes_frontend_entry() -> None:
    connection = _JoinPreflightConnection()
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_CreatePool(connection),
    )

    value = asyncio.run(
        repository.current_game_join_preflight(
            game_id="game-current",
            user_id="user-current",
            agent_id="agent-current",
            key_digest="key-digest",
            request_digest="request-digest",
            now=datetime(2026, 7, 26, 4, tzinfo=timezone.utc),
        )
    )

    assert value["eligible"] is True
    assert value["readyToJoin"] is True
    assert value["joinAuthorizationExpiresAt"] == "2026-07-26T04:10:00+00:00"
    assert value["mandateRequirements"]["expiresAt"] == (
        "2026-07-27T04:00:00+00:00"
    )
    assert value["safeErrorCode"] is None
    assert value["checks"] == {
        "game": "READY",
        "agent": "READY",
        "runtime": "READY",
        "wallet": "READY",
        "paymentMandate": "ACTION_REQUIRED",
    }
    assert value["portfolioRequirements"]["initialPricesAtomic"] == {
        "grain": "2500000",
        "iron": "4500000",
        "warhorse": "7000000",
        "gems": "4000000",
    }
    default_portfolio = value["portfolioRequirements"]["defaultPortfolio"]
    assert (
        int(default_portfolio["cashAtomic"])
        + sum(
            default_portfolio["holdings"][good] * price
            for good, price in {
                "grain": 2_500_000,
                "iron": 4_500_000,
                "warhorse": 7_000_000,
                "gems": 4_000_000,
            }.items()
        )
        == 20_000_000
    )
    assert any(
        "pg_advisory_xact_lock" in query
        for query in connection.queries
    )
    supersede = next(
        query
        for query in connection.queries
        if "UPDATE arena402.join_authorizations" in query
    )
    assert "expires_at <=" not in supersede


def test_join_preflight_rejects_an_existing_user_seat_before_new_authorization() -> None:
    connection = _JoinPreflightConnection()
    connection.existing_participant = {
        "game_participant_id": "gp:game-current:agent-current"
    }
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_CreatePool(connection),
    )

    with pytest.raises(PawnhouseRepositoryError, match="user_already_joined"):
        asyncio.run(
            repository.current_game_join_preflight(
                game_id="game-current",
                user_id="user-current",
                agent_id="agent-current",
                key_digest="key-digest",
                request_digest="request-digest",
            )
        )


def test_join_preflight_rejects_a_full_current_game() -> None:
    connection = _JoinPreflightConnection()
    connection.participant_count = 100
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_CreatePool(connection),
    )

    with pytest.raises(
        PawnhouseRepositoryError,
        match="game_participant_limit_reached",
    ):
        asyncio.run(
            repository.current_game_join_preflight(
                game_id="game-current",
                user_id="user-current",
                agent_id="agent-current",
                key_digest="key-digest",
                request_digest="request-digest",
            )
        )


def test_first_product_sized_game_claims_empty_current_pointer() -> None:
    connection = _CreateConnection()
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_CreatePool(connection),
    )

    asyncio.run(
        repository.create_game(
            game_id="game-current",
            events=build_event_schedule(
                round_count=5,
                seed="current-game-seed",
            ),
            event_seed="current-game-seed",
            max_participants=12,
        )
    )

    assert any(
        "INSERT INTO arena402.current_game" in query
        for query in connection.queries
    )


def test_ensure_current_game_keeps_existing_nonterminal_pointer() -> None:
    connection = _ExistingCurrentConnection()
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_CreatePool(connection),
    )

    result = asyncio.run(
        repository.ensure_current_game(
            game_id="game-unused",
            events=build_event_schedule(
                round_count=5,
                seed="current-game-seed",
                mode="seeded_shuffle",
            ),
            event_seed="current-game-seed",
            market_protocol="agent_a2a.v1",
        )
    )

    assert result == {"gameId": "game-current", "created": False}
    assert any("pg_advisory_xact_lock" in query for query in connection.queries)
    assert not any(
        "INSERT INTO arena402.games" in query
        for query in connection.queries
    )


def test_ensure_current_game_rejects_unknown_market_protocol() -> None:
    connection = _CompletedCurrentConnection()
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_CreatePool(connection),
    )

    with pytest.raises(
        PawnhouseRepositoryError,
        match="invalid_market_protocol",
    ):
        asyncio.run(
            repository.ensure_current_game(
                game_id="game-next",
                events=build_event_schedule(
                    round_count=5,
                    seed="next-current-game-seed",
                    mode="seeded_shuffle",
                ),
                event_seed="next-current-game-seed",
                market_protocol="agent_a2a.latest",
            )
        )


def test_ensure_current_game_creates_and_atomically_rotates_terminal_pointer() -> None:
    connection = _CompletedCurrentConnection()
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_CreatePool(connection),
    )

    result = asyncio.run(
        repository.ensure_current_game(
            game_id="game-next",
            events=build_event_schedule(
                round_count=5,
                seed="next-current-game-seed",
                mode="seeded_shuffle",
            ),
            event_seed="next-current-game-seed",
            market_protocol="agent_a2a.v1",
        )
    )

    assert result["gameId"] == "game-next"
    assert result["created"] is True
    assert result["previousGameId"] == "game-completed"
    assert any(
        "INSERT INTO arena402.games" in query
        for query in connection.queries
    )
    assert any(
        "ON CONFLICT (singleton) DO UPDATE" in query
        for query in connection.queries
    )
    game_insert = next(
        values
        for query, values in connection.fetchval_calls
        if "INSERT INTO arena402.games" in query
    )
    config = json.loads(str(game_insert[6]))
    assert config["portfolioMode"] == "manual"
    assert config["marketProtocol"] == "agent_a2a.v1"
    assert config["priceCatalogId"] == "pawnhouse-price-v1"
    assert config["initialPricesAtomic"] == {
        "grain": "2000000",
        "iron": "5000000",
        "warhorse": "8000000",
        "gems": "3000000",
    }
    assert config["marketFeedbackPolicyVersion"] == (
        "arena.market-feedback.v1"
    )
    assert game_insert[10] == "agent_a2a.v1"


def test_legacy_managed_current_game_does_not_overwrite_joined_portfolios() -> None:
    assert not _should_assign_balanced_portfolios(
        {
            "portfolioMode": "balanced_auto",
            "currentGameManaged": True,
        }
    )
    assert _should_assign_balanced_portfolios(
        {
            "portfolioMode": "balanced_auto",
            "currentGameManaged": False,
        }
    )
