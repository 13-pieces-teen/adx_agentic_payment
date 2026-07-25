from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from arena_game import build_event_schedule
from arena_game.postgres import PostgresPawnhouseRepository


class _Pool:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def fetchrow(self, query: str, *_: object):
        self.queries.append(query)
        return {
            "game_id": "game-current",
            "phase": "portfolio_setup",
            "start_threshold": 10,
            "max_participants": 12,
            "round_count": 5,
            "current_round": 0,
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
                "joined_at": datetime(
                    2026,
                    7,
                    25,
                    10,
                    tzinfo=timezone.utc,
                ),
            }
        ]


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

    def transaction(self):
        return _Transaction()

    async def fetchval(
        self,
        query: str,
        game_id: str | None = None,
        *_: object,
    ):
        self.queries.append(query)
        return game_id

    async def execute(self, query: str, *_: object):
        self.queries.append(query)
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
            "startThreshold": 10,
            "maxParticipants": 12,
            "roundCount": 5,
            "currentRound": 0,
            "roundPhase": None,
            "joinedByMe": False,
            "participants": [
                {
                    "participantId": "gp:game-current:agent-1",
                    "agentId": "agent-1",
                    "displayName": "Merchant Fox",
                    "runtimeKind": "hosted",
                    "readiness": "READY",
                    "joinedAt": "2026-07-25T10:00:00+00:00",
                }
            ],
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
        )
    )

    assert result == {"gameId": "game-current", "created": False}
    assert any("pg_advisory_xact_lock" in query for query in connection.queries)
    assert not any(
        "INSERT INTO arena402.games" in query
        for query in connection.queries
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
