from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.pawnhouse_api import create_pawnhouse_router


class _Repository:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.participants: list[dict[str, object]] = []
        self.submissions: list[dict[str, object]] = []

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

    async def enqueue_hosted_run(self, *, game_id):
        return {
            "gameId": game_id,
            "roundId": f"round:{game_id}:1",
            "runtimeRunId": f"hosted-run:round:{game_id}:1",
            "status": "queued",
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

    async def hosted_run_status(self, *, game_id):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        return {
            "runtime_run_id": f"hosted-run:round:{game_id}:1",
            "round_id": f"round:{game_id}:1",
            "status": "completed",
            "stage": "completed",
            "safe_error_code": None,
            "created_at": now,
            "started_at": now,
            "completed_at": now,
        }

    async def settlement_intents_for_game(self, *, game_id):
        return [
            {
                "settlementIntentId": f"settlement:{game_id}:1",
                "status": "authorization_requested",
            }
        ]

    async def record_settlement_submission(self, **values):
        self.submissions.append(values)
        return {
            "settlementIntentId": values["settlement_intent_id"],
            "status": "submitted",
            "txHash": values["tx_hash"],
        }


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


def test_create_game_freezes_explicit_eip3009_settlement_config() -> None:
    client, repository = _client()
    created = client.post(
        "/api/dev/pawnhouse/games",
        headers={"X-Arena-Dev-Token": "development-token-for-tests"},
        json={
            "gameId": "settlement-game",
            "eventSeed": "fixed-settlement-seed",
            "settlement": {
                "authorizationMode": "single_eip3009",
                "chainId": 1439,
                "tokenAddress": "0x" + "11" * 20,
                "tokenSymbol": "mUSDC",
                "tokenDecimals": 6,
                "requiredConfirmations": 2,
            },
        },
    )
    assert created.status_code == 201
    config = repository.created[0]["settlement_config"]
    assert config.authorization_mode == "single_eip3009"
    assert config.chain_id == 1439
    assert config.required_confirmations == 2


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


def test_hosted_run_queue_is_token_gated_and_status_is_public() -> None:
    client, _ = _client()
    denied = client.post(
        "/api/dev/pawnhouse/games/game_1/run-hosted-market"
    )
    queued = client.post(
        "/api/dev/pawnhouse/games/game_1/run-hosted-market",
        headers={"X-Arena-Dev-Token": "development-token-for-tests"},
    )
    status = client.get(
        "/api/v1/pawnhouse/games/game_1/runtime-run"
    )
    assert denied.status_code == 403
    assert queued.status_code == 202
    assert queued.json()["status"] == "queued"
    assert status.status_code == 200
    assert status.json()["status"] == "completed"


def test_settlement_submission_requires_explicit_observation_and_hides_nonce() -> None:
    client, repository = _client()
    intent_id = "settlement:neg:1"
    path = (
        f"/api/dev/pawnhouse/settlement-intents/{intent_id}/submission"
    )
    body = {
        "txHash": "0x" + "44" * 32,
        "authorizationNonce": "0x" + "55" * 32,
        "submissionSource": "wallet",
        "humanConfirmed": False,
    }
    denied = client.post(
        path,
        headers={"X-Arena-Dev-Token": "development-token-for-tests"},
        json=body,
    )
    assert denied.status_code == 422
    assert denied.json()["detail"]["code"] == "human_confirmation_required"
    assert repository.submissions == []

    body["humanConfirmed"] = True
    accepted = client.post(
        path,
        headers={"X-Arena-Dev-Token": "development-token-for-tests"},
        json=body,
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "submitted"
    assert body["authorizationNonce"] not in accepted.text
    assert len(repository.submissions) == 1


def test_settlement_intent_projection_is_public_and_read_only() -> None:
    client, _ = _client()
    response = client.get(
        "/api/v1/pawnhouse/games/game_1/settlement-intents"
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert (
        response.json()["settlementIntents"][0]["status"]
        == "authorization_requested"
    )
