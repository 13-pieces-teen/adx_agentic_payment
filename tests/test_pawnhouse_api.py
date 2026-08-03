from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena_game import PawnhouseRepositoryError, default_join_portfolio
from connector_gateway.auth import AuthPrincipal
from web.api import create_app
from web.pawnhouse_api import (
    GameStateReadCache,
    SharedGameEventFanout,
    _encode_sse_event,
    _public_game_event_stream,
    _sse_cursor,
    create_pawnhouse_participation_router,
    create_pawnhouse_read_router,
    create_pawnhouse_router,
)


class _Repository:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.participants: list[dict[str, object]] = []
        self.preflights: list[dict[str, object]] = []
        self.approvals: list[dict[str, object]] = []
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

    async def add_connector_participant(self, **values):
        self.participants.append(values)
        return f"gp:{values['game_id']}:{values['agent_id']}"

    async def add_hosted_participant(self, **values):
        self.participants.append(values)
        return f"gp:{values['game_id']}:{values['agent_id']}"

    async def add_current_participant(self, **values):
        self.participants.append(values)
        return f"gp:{values['game_id']}:{values['agent_id']}"

    async def withdraw_current_game_participant(self, **values):
        return {
            "gameId": values["game_id"],
            "participantId": values["participant_id"],
            "withdrawn": True,
        }

    async def start_game(self, *, game_id):
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

    async def enqueue_agent_runtime_run(self, *, game_id):
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

    async def current_game(self, *, owner_user_id=None):
        return {
            "game": {
                "gameId": "game_current",
                "status": "WAITING",
                "readyCount": 1,
                "startThreshold": 2,
                "maxParticipants": 12,
                "roundCount": 5,
                "currentRound": 0,
                "roundPhase": None,
                "joinedByMe": owner_user_id == "user-local",
                "participants": [
                    {
                        "participantId": "gp:game_current:agent-1",
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

    async def current_game_join_preflight(self, **values):
        self.preflights.append(values)
        return {
            "gameId": values["game_id"],
            "agentId": values["agent_id"],
            "eligible": True,
            "readyToJoin": True,
            "joinAuthorizationId": "ja:test",
            "joinAuthorizationExpiresAt": "2026-07-25T10:10:00+00:00",
            "checks": {
                "game": "READY",
                "agent": "READY",
                "runtime": "READY",
                "wallet": "READY",
                "paymentMandate": "ACTION_REQUIRED",
            },
            "mandateRequirements": {
                "chainId": 1439,
                "tokenAddress": "0x" + "11" * 20,
                "tokenSymbol": "mUSDC",
                "tokenDecimals": 6,
                "maxPerPaymentAtomic": "10000000",
                "maxCumulativeAtomic": "50000000",
                "allowedPayeeRule": "SAME_GAME_SETTLEMENT_ACCOUNT",
                "expiresAt": "2026-07-25T10:00:00+00:00",
            },
            "safeErrorCode": None,
            "schemaVersion": "arena.game-join-preflight.v1",
        }

    async def automation_state(self, *, game_id):
        return {
            "gameId": game_id,
            "roundId": f"round:{game_id}:1",
            "action": "wait_settlement",
            "pendingSettlements": 1,
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

    async def record_settlement_approval(self, **values):
        self.approvals.append(values)
        return {
            "settlementIntentId": values["settlement_intent_id"],
            "status": "authorization_requested",
            "intentHash": values["approved_intent_hash"],
            "approvalRecorded": True,
        }

    async def inventory_commit_for_intent(self, *, settlement_intent_id):
        return {
            "settlementIntentId": settlement_intent_id,
            "status": "inventory_committed",
            "inventoryCommitId": f"inventory-commit:{settlement_intent_id}",
            "buyerHoldingBefore": 0,
            "buyerHoldingAfter": 1,
            "sellerHoldingBefore": 1,
            "sellerHoldingAfter": 0,
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


class _Auth:
    async def authenticate(self, _: object) -> AuthPrincipal:
        return AuthPrincipal(
            user_id="user-local",
            username="owner",
            temporary=False,
            session_token_hash="s" * 64,
            csrf_hash="c" * 64,
        )

    async def require_csrf(self, _: object, __: object) -> None:
        return None


def _authenticated_client() -> tuple[TestClient, _Repository]:
    repository = _Repository()
    app = FastAPI()
    app.include_router(
        create_pawnhouse_router(
            repository=repository,  # type: ignore[arg-type]
            dev_token="development-token-for-tests",
            auth=_Auth(),  # type: ignore[arg-type]
        )
    )
    return TestClient(app), repository


def test_read_router_exposes_game_state_without_dev_mutations() -> None:
    repository = _Repository()
    app = FastAPI()
    app.include_router(
        create_pawnhouse_read_router(
            repository=repository,  # type: ignore[arg-type]
        )
    )
    client = TestClient(app)

    state = client.get("/api/v1/pawnhouse/games/game_1")
    mutation = client.post(
        "/api/dev/pawnhouse/games",
        json={
            "gameId": "game_1",
            "eventSeed": "fixed-demo-seed",
        },
    )

    assert state.status_code == 200
    assert state.json()["gameId"] == "game_1"
    assert mutation.status_code == 404


def test_read_router_exposes_anonymous_current_game_projection() -> None:
    repository = _Repository()
    app = FastAPI()
    app.include_router(
        create_pawnhouse_read_router(
            repository=repository,  # type: ignore[arg-type]
        )
    )
    client = TestClient(app)

    response = client.get("/api/v1/games/current")

    assert response.status_code == 200
    assert response.json() == {
        "game": {
            "gameId": "game_current",
            "status": "WAITING",
            "readyCount": 1,
            "startThreshold": 2,
            "maxParticipants": 12,
            "roundCount": 5,
            "currentRound": 0,
            "roundPhase": None,
            "joinedByMe": False,
            "participants": [
                {
                    "participantId": "gp:game_current:agent-1",
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
    assert "userId" not in response.text
    assert "runtimeBindingId" not in response.text
    assert "settlementAccount" not in response.text
    assert response.headers["cache-control"] == "public, max-age=5"
    assert response.headers["vary"] == "Cookie"


def test_current_game_projection_marks_authenticated_owner_joined() -> None:
    repository = _Repository()
    app = FastAPI()
    app.include_router(
        create_pawnhouse_read_router(
            repository=repository,  # type: ignore[arg-type]
            auth=_Auth(),  # type: ignore[arg-type]
        )
    )
    client = TestClient(app)
    client.cookies.set("adx_session", "signed-session")

    response = client.get("/api/v1/games/current")

    assert response.status_code == 200
    assert response.json()["game"]["joinedByMe"] is True


def test_current_game_returns_explicit_not_found_when_pointer_is_empty() -> None:
    class _EmptyRepository(_Repository):
        async def current_game(self, *, owner_user_id=None):
            raise PawnhouseRepositoryError("current_game_not_found")

    app = FastAPI()
    app.include_router(
        create_pawnhouse_read_router(
            repository=_EmptyRepository(),  # type: ignore[arg-type]
        )
    )

    response = TestClient(app).get("/api/v1/games/current")

    assert response.status_code == 404
    assert response.json() == {
        "detail": {"code": "current_game_not_found"}
    }


def test_app_mounts_read_only_game_api_without_dev_control(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ADX_ENV", "development")
    monkeypatch.setenv("ADX_ARENA_CORE_ENABLED", "true")
    monkeypatch.setenv(
        "ADX_ARENA_CORE_DATABASE_URL",
        "postgresql://arena:arena@127.0.0.1:5432/arena",
    )
    monkeypatch.delenv("ADX_ARENA_DEV_CONTROL", raising=False)
    monkeypatch.delenv("ADX_HOSTED_AGENTS_ENABLED", raising=False)
    monkeypatch.delenv("ADX_ARENA_PARTICIPATION_ENABLED", raising=False)

    app = create_app(connector_demo_enabled=False)
    paths = set(app.openapi()["paths"])

    assert "/api/v1/games/current" in paths
    assert "/api/v1/pawnhouse/games/{game_id}" in paths
    assert "/api/v1/ledger/trades" in paths
    assert "/api/v1/ledger/stats" in paths
    assert "/api/dev/pawnhouse/games" not in paths
    assert app.state.pawnhouse_mode == "read_only"


def test_owner_can_add_registered_connector_agent_to_game() -> None:
    client, repository = _authenticated_client()

    response = client.post(
        "/api/v1/pawnhouse/games/game_1/connector-participants",
        json={
            "agentId": "agent-local-1",
            "portfolio": {
                "cash": "20.000000",
                "holdings": {},
            },
            "settlementAccount": {
                "chainId": 1439,
                "address": "0x1111111111111111111111111111111111111111",
                "custodyMode": "wallet",
            },
            "paymentMandateId": "mandate-local-1",
            "joinAuthorizationId": "ja-local-1",
        },
    )

    assert response.status_code == 201
    assert response.json() == {
        "gameId": "game_1",
        "participantId": "gp:game_1:agent-local-1",
        "runtimeKind": "connector",
    }
    assert repository.participants[-1]["user_id"] == "user-local"
    assert repository.participants[-1]["agent_id"] == "agent-local-1"
    assert (
        repository.participants[-1]["settlement_account"].chain_id
        == 1439
    )
    assert (
        repository.participants[-1]["payment_mandate_id"]
        == "mandate-local-1"
    )
    assert (
        repository.participants[-1]["join_authorization_id"]
        == "ja-local-1"
    )


def test_authenticated_participation_router_excludes_dev_controls() -> None:
    repository = _Repository()
    app = FastAPI()
    app.include_router(
        create_pawnhouse_participation_router(
            repository=repository,  # type: ignore[arg-type]
            auth=_Auth(),  # type: ignore[arg-type]
        )
    )
    client = TestClient(app)

    joined = client.post(
        "/api/v1/pawnhouse/games/game_1/connector-participants",
        json={
            "agentId": "agent-local-1",
            "portfolio": {"cash": "20.000000", "holdings": {}},
        },
    )
    dev_mutation = client.post(
        "/api/dev/pawnhouse/games",
        json={"gameId": "game_1", "eventSeed": "fixed-demo-seed"},
    )

    assert joined.status_code == 201
    assert dev_mutation.status_code == 404


def test_current_game_join_preflight_is_authenticated_and_idempotent() -> None:
    repository = _Repository()
    app = FastAPI()
    app.include_router(
        create_pawnhouse_participation_router(
            repository=repository,  # type: ignore[arg-type]
            auth=_Auth(),  # type: ignore[arg-type]
        )
    )

    response = TestClient(app).post(
        "/api/v1/games/game_current/join-preflight",
        headers={"Idempotency-Key": "preflight-key-0001"},
        json={"agentId": "agent-hosted-1"},
    )

    assert response.status_code == 200
    assert response.json()["eligible"] is True
    assert response.json()["readyToJoin"] is True
    assert response.json()["joinAuthorizationExpiresAt"] == (
        "2026-07-25T10:10:00+00:00"
    )
    assert response.json()["joinAuthorizationId"] == "ja:test"
    assert response.json()["mandateRequirements"]["allowedPayeeRule"] == (
        "SAME_GAME_SETTLEMENT_ACCOUNT"
    )
    assert repository.preflights[-1]["user_id"] == "user-local"
    assert repository.preflights[-1]["agent_id"] == "agent-hosted-1"


def test_current_game_join_requires_ready_mandate_and_returns_authoritative_status() -> None:
    repository = _Repository()
    app = FastAPI()
    app.include_router(
        create_pawnhouse_participation_router(
            repository=repository,  # type: ignore[arg-type]
            auth=_Auth(),  # type: ignore[arg-type]
        )
    )

    response = TestClient(app).post(
        "/api/v1/games/game_current/participants",
        headers={"x-csrf-token": "csrf"},
        json={
            "agentId": "agent-current",
            "paymentMandateId": "mandate-current",
            "joinAuthorizationId": "ja-current",
        },
    )

    assert response.status_code == 201, response.text
    expected_portfolio = default_join_portfolio(
        game_id="game_current",
        agent_id="agent-current",
    )
    assert response.json() == {
        "gameId": "game_current",
        "participantId": "gp:game_current:agent-current",
        "readiness": "READY",
        "status": "WAITING",
        "readyCount": 1,
        "startThreshold": 2,
        "initialPortfolio": {
            "cashAtomic": str(expected_portfolio.cash_atomic),
            "holdings": expected_portfolio.holdings,
        },
        "schemaVersion": "arena.game-join.v2",
    }
    joined = repository.participants[0]
    assert joined["require_current_game"] is True
    assert joined["payment_mandate_id"] == "mandate-current"
    assert joined["join_authorization_id"] == "ja-current"
    assert joined["portfolio"] == expected_portfolio


def test_current_game_join_accepts_an_exact_twenty_gold_custom_portfolio() -> None:
    repository = _Repository()
    app = FastAPI()
    app.include_router(
        create_pawnhouse_participation_router(
            repository=repository,  # type: ignore[arg-type]
            auth=_Auth(),  # type: ignore[arg-type]
        )
    )

    response = TestClient(app).post(
        "/api/v1/games/game_current/participants",
        headers={"x-csrf-token": "csrf"},
        json={
            "agentId": "agent-current",
            "paymentMandateId": "mandate-current",
            "joinAuthorizationId": "ja-current",
            "portfolio": {
                "cashAtomic": "2000000",
                "holdings": {
                    "grain": 1,
                    "iron": 1,
                    "warhorse": 1,
                    "gems": 1,
                },
            },
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["initialPortfolio"] == {
        "cashAtomic": "2000000",
        "holdings": {
            "grain": 1,
            "iron": 1,
            "warhorse": 1,
            "gems": 1,
        },
    }
    portfolio = repository.participants[0]["portfolio"]
    assert portfolio.cash_atomic == 2_000_000
    assert portfolio.holdings == {
        "grain": 1,
        "iron": 1,
        "warhorse": 1,
        "gems": 1,
    }


def test_current_game_join_rejects_a_portfolio_that_mints_extra_value() -> None:
    repository = _Repository()
    app = FastAPI()
    app.include_router(
        create_pawnhouse_participation_router(
            repository=repository,  # type: ignore[arg-type]
            auth=_Auth(),  # type: ignore[arg-type]
        )
    )

    response = TestClient(app).post(
        "/api/v1/games/game_current/participants",
        headers={"x-csrf-token": "csrf"},
        json={
            "agentId": "agent-current",
            "paymentMandateId": "mandate-current",
            "joinAuthorizationId": "ja-current",
            "portfolio": {
                "cashAtomic": "20000000",
                "holdings": {"warhorse": 1},
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_portfolio"
    assert repository.participants == []


def test_current_game_withdraw_is_authenticated_and_owner_scoped() -> None:
    repository = _Repository()
    app = FastAPI()
    app.include_router(
        create_pawnhouse_participation_router(
            repository=repository,  # type: ignore[arg-type]
            auth=_Auth(),  # type: ignore[arg-type]
        )
    )
    response = TestClient(app).delete(
        "/api/v1/games/game_current/participants/gp:game_current:agent-current",
        headers={"x-csrf-token": "csrf"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["withdrawn"] is True


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


def test_create_game_accepts_an_eight_round_seeded_event_deck() -> None:
    client, repository = _client()
    created = client.post(
        "/api/dev/pawnhouse/games",
        headers={"X-Arena-Dev-Token": "development-token-for-tests"},
        json={
            "gameId": "eight-round-game",
            "eventSeed": "eight-round-fixed-seed",
            "roundCount": 8,
            "eventDeckId": "pawnhouse-standard-v1",
            "eventMode": "seeded_shuffle",
        },
    )

    assert created.status_code == 201
    events = repository.created[0]["events"]
    assert len(events) == 8
    assert [event.reveal_round for event in events] == list(range(1, 9))
    assert len({event.event_id for event in events}) == 8
    assert repository.created[0]["event_deck_id"] == "pawnhouse-standard-v1"
    assert repository.created[0]["event_mode"] == "seeded_shuffle"


def test_fixed_demo_event_mode_rejects_a_non_five_round_game() -> None:
    client, repository = _client()
    created = client.post(
        "/api/dev/pawnhouse/games",
        headers={"X-Arena-Dev-Token": "development-token-for-tests"},
        json={
            "gameId": "invalid-fixed-demo",
            "eventSeed": "invalid-fixed-demo-seed",
            "roundCount": 8,
        },
    )

    assert created.status_code == 422
    assert created.json()["detail"]["code"] == (
        "fixed_demo_requires_exactly_five_rounds"
    )
    assert repository.created == []


def test_create_game_freezes_a_twelve_agent_participant_limit() -> None:
    client, repository = _client()
    created = client.post(
        "/api/dev/pawnhouse/games",
        headers={"X-Arena-Dev-Token": "development-token-for-tests"},
        json={
            "gameId": "twelve-agent-game",
            "eventSeed": "twelve-agent-fixed-seed",
            "maxParticipants": 12,
        },
    )

    assert created.status_code == 201
    assert repository.created[0]["max_participants"] == 12


def test_create_game_accepts_a_two_hundred_agent_participant_limit() -> None:
    client, repository = _client()
    created = client.post(
        "/api/dev/pawnhouse/games",
        headers={"X-Arena-Dev-Token": "development-token-for-tests"},
        json={
            "gameId": "two-hundred-agent-game",
            "eventSeed": "two-hundred-agent-fixed-seed",
            "maxParticipants": 200,
        },
    )

    assert created.status_code == 201
    assert repository.created[0]["max_participants"] == 200


def test_start_game_uses_the_schedule_persisted_at_creation() -> None:
    client, _ = _client()
    started = client.post(
        "/api/dev/pawnhouse/games/eight-round-game/start",
        headers={"X-Arena-Dev-Token": "development-token-for-tests"},
    )

    assert started.status_code == 200
    assert started.json()["phase"] == "decide"


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
                "tokenEip712Name": "Mock USD Coin",
                "tokenEip712Version": "1",
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

    automation = client.get("/api/v1/pawnhouse/games/game_1/automation")
    assert automation.status_code == 200
    assert automation.json()["action"] == "wait_settlement"


def test_public_game_event_stream_resumes_and_emits_sanitized_json() -> None:
    repository = _Repository()
    disconnect_checks = 0

    async def disconnected() -> bool:
        nonlocal disconnect_checks
        disconnect_checks += 1
        return disconnect_checks > 1

    async def exercise() -> tuple[str, bool]:
        stream = _public_game_event_stream(
            repository=repository,
            game_id="game_1",
            after_sequence=_sse_cursor(2, "4"),
            is_disconnected=disconnected,
            poll_seconds=0.001,
            heartbeat_seconds=1,
        )
        first = await anext(stream)
        try:
            await anext(stream)
        except StopAsyncIteration:
            return first, True
        return first, False

    encoded, stopped = asyncio.run(exercise())
    assert stopped is True
    assert encoded.startswith("id: 5\nevent: arena\ndata: ")
    payload = json.loads(encoded.split("data: ", 1)[1])
    assert payload == {
        "sequence": 5,
        "type": "game.created",
        "data": {},
    }
    assert _sse_cursor(3, "invalid") == 3
    assert _encode_sse_event(payload) == encoded


def test_sse_subscribers_share_one_ongoing_game_poller() -> None:
    class FanoutRepository:
        def __init__(self):
            self.calls = 0
            self.release = asyncio.Event()

        async def timeline(self, game_id, *, after_sequence=0):
            assert game_id == "game_1"
            self.calls += 1
            task = asyncio.current_task()
            if task is not None and task.get_name().startswith(
                "arena-sse-fanout:"
            ):
                if after_sequence >= 1:
                    await asyncio.Event().wait()
                await self.release.wait()
                return [
                    {
                        "sequence": 1,
                        "type": "round.started",
                        "data": {},
                    }
                ]
            return []

    async def exercise() -> tuple[str, str, int]:
        repository = FanoutRepository()
        fanout = SharedGameEventFanout(
            repository,
            poll_seconds=0.001,
            heartbeat_seconds=10,
        )

        async def connected() -> bool:
            return False

        first = fanout.stream(
            game_id="game_1",
            after_sequence=0,
            is_disconnected=connected,
        )
        second = fanout.stream(
            game_id="game_1",
            after_sequence=0,
            is_disconnected=connected,
        )
        first_frame = asyncio.create_task(anext(first))
        second_frame = asyncio.create_task(anext(second))
        while repository.calls < 3:
            await asyncio.sleep(0)
        calls_before_release = repository.calls
        repository.release.set()
        frames = await asyncio.gather(first_frame, second_frame)
        await first.aclose()
        await second.aclose()
        await fanout.close()
        return frames[0], frames[1], calls_before_release

    first, second, calls = asyncio.run(exercise())
    assert first == second
    assert first.startswith("id: 1\nevent: arena\n")
    # Two one-time replay reads plus one shared ongoing poll.
    assert calls == 3


def test_game_state_cache_collapses_concurrent_projection_reads() -> None:
    class ProjectionRepository:
        def __init__(self):
            self.calls = 0
            self.release = asyncio.Event()

        async def game_state(self, game_id):
            self.calls += 1
            await self.release.wait()
            return {"gameId": game_id, "phase": "running"}

    async def exercise():
        repository = ProjectionRepository()
        cache = GameStateReadCache(repository, ttl_seconds=1)
        first = asyncio.create_task(cache.get("game_1"))
        second = asyncio.create_task(cache.get("game_1"))
        while repository.calls < 1:
            await asyncio.sleep(0)
        repository.release.set()
        values = await asyncio.gather(first, second)
        third = await cache.get("game_1")
        return repository.calls, values, third

    calls, values, third = asyncio.run(exercise())
    assert calls == 1
    assert values == [
        {"gameId": "game_1", "phase": "running"},
        {"gameId": "game_1", "phase": "running"},
    ]
    assert third == values[0]


def test_hosted_run_queue_is_token_gated_and_status_is_public() -> None:
    client, _ = _client()
    denied = client.post("/api/dev/pawnhouse/games/game_1/run-hosted-market")
    queued = client.post(
        "/api/dev/pawnhouse/games/game_1/run-hosted-market",
        headers={"X-Arena-Dev-Token": "development-token-for-tests"},
    )
    status = client.get("/api/v1/pawnhouse/games/game_1/runtime-run")
    assert denied.status_code == 403
    assert queued.status_code == 202
    assert queued.json()["status"] == "queued"
    assert status.status_code == 200
    assert status.json()["status"] == "completed"


def test_settlement_submission_requires_explicit_observation_and_hides_nonce() -> None:
    client, repository = _client()
    intent_id = "settlement:neg:1"
    path = f"/api/dev/pawnhouse/settlement-intents/{intent_id}/submission"
    body = {
        "txHash": "0x" + "44" * 32,
        "authorizationNonce": "0x" + "55" * 32,
        "approvedIntentHash": "sha256:" + "66" * 32,
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
    assert repository.submissions[0]["approved_intent_hash"] == ("sha256:" + "66" * 32)


def test_settlement_approval_is_recorded_before_broadcast_and_bound_to_hash() -> None:
    client, repository = _client()
    intent_id = "settlement:neg:1"
    intent_hash = "sha256:" + "44" * 32
    path = f"/api/dev/pawnhouse/settlement-intents/{intent_id}/approval"
    body = {
        "approvedIntentHash": intent_hash,
        "authorizationNonce": "0x" + "44" * 32,
        "approvalSource": "operator_cli",
        "humanConfirmed": False,
    }

    denied = client.post(
        path,
        headers={"X-Arena-Dev-Token": "development-token-for-tests"},
        json=body,
    )
    assert denied.status_code == 422
    assert denied.json()["detail"]["code"] == "human_confirmation_required"
    assert repository.approvals == []

    body["humanConfirmed"] = True
    approved = client.post(
        path,
        headers={"X-Arena-Dev-Token": "development-token-for-tests"},
        json=body,
    )
    assert approved.status_code == 200
    assert approved.json()["approvalRecorded"] is True
    assert body["authorizationNonce"] not in approved.text
    assert repository.approvals == [
        {
            "settlement_intent_id": intent_id,
            "approved_intent_hash": intent_hash,
            "authorization_nonce": "0x" + "44" * 32,
            "approval_source": "operator_cli",
        }
    ]


def test_settlement_intent_projection_is_public_and_read_only() -> None:
    client, _ = _client()
    response = client.get("/api/v1/pawnhouse/games/game_1/settlement-intents")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert (
        response.json()["settlementIntents"][0]["status"] == "authorization_requested"
    )


def test_inventory_commit_receipt_is_public_and_stable() -> None:
    client, _ = _client()
    intent_id = "settlement:neg:1"
    response = client.get(
        f"/api/v1/pawnhouse/settlement-intents/{intent_id}/inventory-commit"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "inventory_committed"
    assert response.json()["inventoryCommitId"] == (f"inventory-commit:{intent_id}")
