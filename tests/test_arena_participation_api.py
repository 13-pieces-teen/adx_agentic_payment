from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena_core import ArenaParticipationError, GameParticipation
from connector_gateway.auth import AuthPrincipal
from web.arena_participation_api import create_arena_participation_router


class _Auth:
    async def authenticate(self, _: object) -> AuthPrincipal:
        return AuthPrincipal(
            user_id="user-1",
            username="owner",
            temporary=False,
            session_token_hash="s" * 64,
            csrf_hash="c" * 64,
        )

    async def require_csrf(self, _: object, __: object) -> None:
        return None


class _Repository:
    def __init__(self) -> None:
        self.join_values: dict[str, str] | None = None
        self.error: str | None = None

    async def join(self, **values: str) -> GameParticipation:
        self.join_values = values
        if self.error is not None:
            raise ArenaParticipationError(self.error)
        return GameParticipation(
            game_agent_id="gagent-1",
            game_id=values["game_id"],
            agent_id=values["agent_id"],
            runtime_binding_id="rbind-1",
            runtime_kind="hosted",
            status="joined",
            config_hash="sha256:" + "a" * 64,
        )

    async def list_for_owner(
        self,
        owner_user_id: str,
    ) -> tuple[GameParticipation, ...]:
        del owner_user_id
        return ()


def _client(repository: _Repository) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_arena_participation_router(
            auth=_Auth(),  # type: ignore[arg-type]
            repository=repository,  # type: ignore[arg-type]
        )
    )
    return TestClient(app)


def test_join_is_owner_scoped_and_hashes_idempotency_key() -> None:
    repository = _Repository()
    client = _client(repository)
    raw_key = "join-request-00000001"

    response = client.post(
        "/api/games/game-1/participants",
        headers={"Idempotency-Key": raw_key},
        json={"agentId": "agent-1"},
    )

    assert response.status_code == 201
    assert response.json()["runtimeKind"] == "hosted"
    assert repository.join_values is not None
    assert repository.join_values["owner_user_id"] == "user-1"
    assert repository.join_values["key_digest"].startswith("sha256:")
    assert raw_key not in repr(repository.join_values)
    assert repository.join_values["request_digest"].startswith("sha256:")


def test_join_rejects_a_second_agent_for_the_same_game() -> None:
    repository = _Repository()
    repository.error = "user_already_joined"

    response = _client(repository).post(
        "/api/games/game-1/participants",
        headers={"Idempotency-Key": "join-request-00000001"},
        json={"agentId": "agent-2"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {"code": "user_already_joined"}
    }


def test_list_participations_requires_owner_scope_query() -> None:
    response = _client(_Repository()).get(
        "/api/game-participations?scope=all"
    )

    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "invalid_request"}}
