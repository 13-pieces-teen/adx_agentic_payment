from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from arena_game.official_filler import (
    OfficialAgentFiller,
    official_seat_deficit,
)
from arena_game.postgres import PostgresPawnhouseRepository


class _Repository:
    def __init__(self, plan: dict[str, object]) -> None:
        self.plan = plan
        self.joined: list[tuple[str, str]] = []

    async def official_fill_plan(
        self,
        *,
        now: datetime,
    ) -> dict[str, object]:
        assert now.tzinfo is not None
        return self.plan

    async def add_official_hosted_participant(
        self,
        *,
        game_id: str,
        agent_id: str,
    ) -> str:
        self.joined.append((game_id, agent_id))
        return f"gp:{game_id}:{agent_id}"


def test_filler_waits_for_server_deadline() -> None:
    repository = _Repository(
        {
            "gameId": "game-current",
            "status": "COLLECTING",
            "candidateAgentIds": [],
        }
    )
    filler = OfficialAgentFiller(repository=repository)  # type: ignore[arg-type]

    result = asyncio.run(
        filler.run_once(now=datetime(2026, 7, 26, tzinfo=timezone.utc))
    )

    assert result["status"] == "COLLECTING"
    assert result["filledCount"] == 0
    assert repository.joined == []


def test_filler_joins_only_server_selected_official_agents() -> None:
    repository = _Repository(
        {
            "gameId": "game-current",
            "status": "FILLING",
            "candidateAgentIds": ["official-01", "official-02"],
        }
    )
    filler = OfficialAgentFiller(repository=repository)  # type: ignore[arg-type]

    result = asyncio.run(
        filler.run_once(now=datetime(2026, 7, 26, tzinfo=timezone.utc))
    )

    assert repository.joined == [
        ("game-current", "official-01"),
        ("game-current", "official-02"),
    ]
    assert result["status"] == "FILLING"
    assert result["filledCount"] == 2


def test_pending_participants_reserve_official_fill_seats() -> None:
    assert official_seat_deficit(
        target_seats=10,
        ready_count=1,
        participating_count=10,
    ) == 0
    assert official_seat_deficit(
        target_seats=10,
        ready_count=1,
        participating_count=4,
    ) == 6


def test_one_player_gets_nine_stably_random_official_identities() -> None:
    class _Pool:
        def __init__(self) -> None:
            self.fetchrow_count = 0
            self.selection_sql = ""
            self.selection_parameters: tuple[object, ...] = ()

        async def fetchrow(
            self,
            _: str,
            *parameters: object,
        ) -> dict[str, object]:
            self.fetchrow_count += 1
            if self.fetchrow_count == 1:
                return {
                    "game_id": "game-randomized-officials",
                    "phase": "registration",
                    "config_snapshot": {
                        "officialFillAfterSeconds": 300,
                    },
                    "start_threshold": 10,
                }
            assert parameters == ("game-randomized-officials",)
            return {
                "participating_count": 1,
                "ready_count": 1,
                "first_human_ready_at": (
                    datetime(2026, 8, 5, tzinfo=timezone.utc)
                    - timedelta(seconds=301)
                ),
            }

        async def fetch(
            self,
            sql: str,
            *parameters: object,
        ) -> list[dict[str, str]]:
            self.selection_sql = sql
            self.selection_parameters = parameters
            return [
                {"agent_id": f"official-{index:02d}"}
                for index in range(1, 10)
            ]

    async def scenario() -> None:
        pool = _Pool()
        repository = PostgresPawnhouseRepository("", pool=pool)

        plan = await repository.official_fill_plan(
            now=datetime(2026, 8, 5, tzinfo=timezone.utc)
        )

        assert plan["status"] == "FILLING"
        assert plan["candidateAgentIds"] == [
            f"official-{index:02d}" for index in range(1, 10)
        ]
        assert pool.selection_parameters == (
            "game-randomized-officials",
            9,
        )
        assert "md5(" in pool.selection_sql
        assert "$1::text || ':' || official.agent_id" in pool.selection_sql
        assert "arena.official-selection.v1" in pool.selection_sql

    asyncio.run(scenario())


def test_one_ready_player_immediately_requests_thirty_one_for_target_32() -> None:
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)

    class _ImmediatePool:
        def __init__(self) -> None:
            self.fetchrow_count = 0
            self.selection_parameters: tuple[object, ...] = ()

        async def fetchrow(
            self,
            _: str,
            *parameters: object,
        ) -> dict[str, object]:
            self.fetchrow_count += 1
            if self.fetchrow_count == 1:
                return {
                    "game_id": "game-target-32",
                    "phase": "registration",
                    "config_snapshot": {
                        "officialFillAfterSeconds": 0,
                    },
                    "start_threshold": 32,
                }
            assert parameters == ("game-target-32",)
            return {
                "participating_count": 1,
                "ready_count": 1,
                "first_human_ready_at": now,
            }

        async def fetch(
            self,
            _: str,
            *parameters: object,
        ) -> list[dict[str, str]]:
            self.selection_parameters = parameters
            return [
                {"agent_id": f"official-{index:02d}"}
                for index in range(1, 32)
            ]

    async def scenario() -> None:
        pool = _ImmediatePool()
        repository = PostgresPawnhouseRepository("", pool=pool)

        plan = await repository.official_fill_plan(now=now)

        assert plan["status"] == "FILLING"
        assert len(plan["candidateAgentIds"]) == 31
        assert pool.selection_parameters == ("game-target-32", 31)

    asyncio.run(scenario())
