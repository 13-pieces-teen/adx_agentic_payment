from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from connector_gateway.auth import AuthPrincipal
from web.game_operator_api import create_game_operator_router


class _Auth:
    async def authenticate(self, _: object) -> AuthPrincipal:
        return AuthPrincipal(
            user_id="user-1",
            username="operator",
            temporary=False,
            session_token_hash="s" * 64,
            csrf_hash="c" * 64,
        )

    async def require_csrf(self, _: object, __: object) -> None:
        return None


class _Repository:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.listed = False
        self.started: dict[str, object] | None = None

    async def create_game(self, **values: object) -> dict[str, object]:
        self.created.append(values)
        return {
            "gameId": values["game_id"],
            "phase": "registration",
            "eventScheduleCommitment": "sha256:" + "a" * 64,
        }

    async def list_games(self, *, limit: int) -> list[dict[str, object]]:
        self.listed = True
        assert limit == 50
        return [
            {
                "gameId": "production-game-1",
                "phase": "registration",
                "roundCount": 5,
                "currentRound": 0,
                "participantCount": 0,
                "maxParticipants": 16,
                "createdAt": "2026-07-25T00:00:00Z",
            }
        ]

    async def start_game(self, **values: object) -> dict[str, object]:
        self.started = values
        return {
            "gameId": values["game_id"],
            "phase": "running",
            "roundId": "round:production-game-1:1",
        }


def test_authenticated_operator_can_create_a_no_payment_game() -> None:
    repository = _Repository()
    app = FastAPI()
    app.include_router(
        create_game_operator_router(
            auth=_Auth(),  # type: ignore[arg-type]
            repository=repository,  # type: ignore[arg-type]
        )
    )

    response = TestClient(app).post(
        "/api/v1/pawnhouse/games",
        json={
            "gameId": "production-game-1",
            "eventSeed": "server-reviewed-seed",
            "settlement": {"authorizationMode": "none"},
        },
    )

    assert response.status_code == 201
    assert response.json()["gameId"] == "production-game-1"
    assert repository.created[0]["operator_user_id"] == "user-1"
    assert (
        repository.created[0]["settlement_config"].authorization_mode
        == "none"
    )


def test_authenticated_user_can_list_games_without_a_known_game_id() -> None:
    repository = _Repository()
    app = FastAPI()
    app.include_router(
        create_game_operator_router(
            auth=_Auth(),  # type: ignore[arg-type]
            repository=repository,  # type: ignore[arg-type]
        )
    )

    response = TestClient(app).get("/api/v1/pawnhouse/games")

    assert response.status_code == 200
    assert response.json() == {
        "games": [
            {
                "gameId": "production-game-1",
                "phase": "registration",
                "roundCount": 5,
                "currentRound": 0,
                "participantCount": 0,
                "maxParticipants": 16,
                "createdAt": "2026-07-25T00:00:00Z",
            }
        ],
        "total": 1,
        "schemaVersion": "arena.pawnhouse-game-list.v1",
    }
    assert repository.listed is True


def test_only_the_authenticated_creator_identity_is_sent_to_start() -> None:
    repository = _Repository()
    app = FastAPI()
    app.include_router(
        create_game_operator_router(
            auth=_Auth(),  # type: ignore[arg-type]
            repository=repository,  # type: ignore[arg-type]
        )
    )

    response = TestClient(app).post(
        "/api/v1/pawnhouse/games/production-game-1/start"
    )

    assert response.status_code == 200
    assert response.json()["phase"] == "running"
    assert repository.started == {
        "game_id": "production-game-1",
        "operator_user_id": "user-1",
    }
