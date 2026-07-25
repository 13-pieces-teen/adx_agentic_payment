from __future__ import annotations

import asyncio

from arena_game.postgres import PostgresPawnhouseRepository


class _Acquire:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection

    async def __aenter__(self) -> "_Connection":
        return self.connection

    async def __aexit__(self, *_: object) -> bool:
        return False


class _Pool:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


class _Connection:
    def __init__(
        self,
        *,
        runtime_run: dict[str, object] | None = None,
    ) -> None:
        self.runtime_run = runtime_run

    async def fetchrow(self, query: str, *_: object):
        if "FROM arena402.games" in query:
            return {"phase": "running", "current_round": 1}
        if "FROM arena402.rounds" in query:
            return {
                "round_id": "round:game-1:1",
                "round_index": 1,
                "phase": "decide",
            }
        if "FROM arena402.runtime_runs" in query:
            return self.runtime_run
        raise AssertionError(query)

    async def fetch(self, query: str, *_: object):
        if "FROM arena402.game_participants" in query:
            return [
                {"runtime_kind": "connector", "participant_count": 1},
                {"runtime_kind": "hosted", "participant_count": 1},
            ]
        raise AssertionError(query)

    async def fetchval(self, query: str, *_: object):
        if "FROM arena402.negotiations" in query:
            return 0
        if "FROM arena402.pairings" in query:
            return 0
        raise AssertionError(query)


def test_mixed_hosted_connector_round_queues_one_agent_runtime_run() -> None:
    connection = _Connection()
    repository = PostgresPawnhouseRepository(
        "",
        pool=_Pool(connection),
    )

    state = asyncio.run(repository.automation_state(game_id="game-1"))

    assert state["runtimeKinds"] == {"connector": 1, "hosted": 1}
    assert state["action"] == "enqueue_agent_runtime"


def test_mixed_round_waits_for_its_existing_runtime_run() -> None:
    connection = _Connection(
        runtime_run={
            "status": "running",
            "stage": "decide",
            "safe_error_code": None,
        }
    )
    repository = PostgresPawnhouseRepository(
        "",
        pool=_Pool(connection),
    )

    state = asyncio.run(repository.automation_state(game_id="game-1"))

    assert state["action"] == "wait_runtime"
    assert state["runtimeRunStatus"] == "running"
