from __future__ import annotations

import asyncio

from arena_game.orchestrator import PawnhouseGameOrchestrator


class _Repository:
    def __init__(self, states: dict[str, dict[str, object]]) -> None:
        self.states = states
        self.queued: list[str] = []
        self.rule_runs: list[str] = []
        self.advanced: list[str] = []

    async def automatable_game_ids(self, *, limit: int) -> list[str]:
        assert limit == 50
        return list(self.states)

    async def automation_state(self, *, game_id: str) -> dict[str, object]:
        return self.states[game_id]

    async def enqueue_agent_runtime_run(self, *, game_id: str) -> object:
        self.queued.append(game_id)
        return {}

    async def run_rule_market(self, *, game_id: str) -> object:
        self.rule_runs.append(game_id)
        return {}

    async def advance_round_or_game(self, *, game_id: str) -> object:
        self.advanced.append(game_id)
        return {}


def test_running_agent_game_queues_its_decide_round_automatically() -> None:
    repository = _Repository(
        {
            "game-1": {
                "action": "enqueue_agent_runtime",
                "roundId": "round:game-1:1",
            }
        }
    )
    orchestrator = PawnhouseGameOrchestrator(repository=repository)

    assert asyncio.run(orchestrator.run_once()) == 1
    assert repository.queued == ["game-1"]
    assert repository.rule_runs == []
    assert repository.advanced == []


def test_mixed_hosted_connector_game_uses_one_runtime_run() -> None:
    repository = _Repository(
        {
            "game-1": {
                "action": "enqueue_agent_runtime",
                "roundId": "round:game-1:1",
                "runtimeKinds": {"hosted": 1, "connector": 1},
            }
        }
    )
    orchestrator = PawnhouseGameOrchestrator(repository=repository)

    assert asyncio.run(orchestrator.run_once()) == 1
    assert repository.queued == ["game-1"]
    assert repository.rule_runs == []
    assert repository.advanced == []


def test_running_rule_game_executes_its_decide_round_automatically() -> None:
    repository = _Repository(
        {
            "game-1": {
                "action": "run_rule",
                "roundId": "round:game-1:1",
            }
        }
    )
    orchestrator = PawnhouseGameOrchestrator(repository=repository)

    assert asyncio.run(orchestrator.run_once()) == 1
    assert repository.rule_runs == ["game-1"]
    assert repository.queued == []
    assert repository.advanced == []


def test_pending_settlement_is_observed_without_advancing_the_game() -> None:
    repository = _Repository(
        {
            "game-1": {
                "action": "wait_settlement",
                "roundId": "round:game-1:1",
            }
        }
    )
    orchestrator = PawnhouseGameOrchestrator(repository=repository)

    assert asyncio.run(orchestrator.run_once()) == 0
    assert repository.queued == []
    assert repository.rule_runs == []
    assert repository.advanced == []


def test_terminal_market_advances_exactly_one_round_transition() -> None:
    repository = _Repository(
        {
            "game-1": {
                "action": "advance_round",
                "roundId": "round:game-1:1",
            }
        }
    )
    orchestrator = PawnhouseGameOrchestrator(repository=repository)

    assert asyncio.run(orchestrator.run_once()) == 1
    assert repository.advanced == ["game-1"]
    assert repository.queued == []
    assert repository.rule_runs == []
