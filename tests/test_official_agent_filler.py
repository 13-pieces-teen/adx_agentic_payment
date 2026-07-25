from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from arena_game.official_filler import OfficialAgentFiller


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
