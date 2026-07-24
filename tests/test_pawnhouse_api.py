from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.pawnhouse_api import create_pawnhouse_router


class _Repository:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.participants: list[dict[str, object]] = []

    async def create_game(self, **values):
        self.created.append(values)
        return {
            "gameId": values["game_id"],
            "phase": "registration",
            "eventScheduleCommitment": "sha256:" + "0" * 64,
        }

    async def add_rule_participant(self, **values):
        self.participants.append(values)
        return f"gp:{values['game_id']}:{values['agent_id']}"

    async def start_game(self, *, game_id, events):
        return {
            "gameId": game_id,
            "roundId": f"round:{game_id}:1",
            "phase": "decide",
        }

    async def run_rule_market(self, *, game_id):
        return {
            "gameId": game_id,
            "decisions": [],
            "pairings": [],
            "negotiations": [],
        }

    async def game_state(self, game_id):
        return {
            "gameId": game_id,
            "phase": "running",
            "schemaVersion": "arena.pawnhouse-game-state.v1",
        }

    async def timeline(self, game_id, *, after_sequence=0):
        return [
            {
                "sequence": after_sequence + 1,
                "type": "game.created",
                "data": {},
            }
        ]


def _client() -> tuple[TestClient, _Repository]:
    repository = _Repository()
    app = FastAPI()
    app.include_router(
        create_pawnhouse_router(
            repository=repository,  # type: ignore[arg-type]
            dev_token="development-token-for-tests",
        )
    )
    return TestClient(app), repository


def test_development_mutations_require_the_explicit_token() -> None:
    client, _ = _client()
    response = client.post(
        "/api/dev/pawnhouse/games",
        json={
            "gameId": "game_1",
            "eventSeed": "fixed-demo-seed",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "invalid_development_token"


def test_create_game_and_add_twenty_gold_rule_participant() -> None:
    client, repository = _client()
    headers = {"X-Arena-Dev-Token": "development-token-for-tests"}
    created = client.post(
        "/api/dev/pawnhouse/games",
        headers=headers,
        json={
            "gameId": "game_1",
            "eventSeed": "fixed-demo-seed",
        },
    )
    assert created.status_code == 201
    assert created.json()["phase"] == "registration"

    participant = client.post(
        "/api/dev/pawnhouse/games/game_1/rule-participants",
        headers=headers,
        json={
            "userId": "user_1",
            "agentId": "agent_1",
            "portfolio": {
                "cash": "0",
                "holdings": {
                    "grain": 2,
                    "iron": 1,
                    "warhorse": 1,
                    "gems": 1,
                },
            },
            "strategy": {
                "intent": "sell",
                "good": "iron",
                "targetPrice": "6",
                "publicMessage": "六金即可交货。",
            },
        },
    )
    assert participant.status_code == 201
    assert participant.json()["runtimeKind"] == "rule"
    assert len(repository.created) == 1
    assert len(repository.participants) == 1


def test_invalid_initial_portfolio_is_rejected_before_repository_write() -> None:
    client, repository = _client()
    response = client.post(
        "/api/dev/pawnhouse/games/game_1/rule-participants",
        headers={"X-Arena-Dev-Token": "development-token-for-tests"},
        json={
            "userId": "user_1",
            "agentId": "agent_1",
            "portfolio": {"cash": "19", "holdings": {}},
            "strategy": {
                "intent": "buy",
                "good": "iron",
                "targetPrice": "7",
                "publicMessage": "七金以内成交。",
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_portfolio"
    assert repository.participants == []


def test_read_interfaces_do_not_require_the_development_token() -> None:
    client, _ = _client()
    state = client.get("/api/v1/pawnhouse/games/game_1")
    timeline = client.get(
        "/api/v1/pawnhouse/games/game_1/timeline",
        params={"after": 4},
    )
    assert state.status_code == 200
    assert state.json()["phase"] == "running"
    assert timeline.status_code == 200
    assert timeline.json()["nextAfter"] == 5

