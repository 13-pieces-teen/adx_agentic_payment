from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena_core import (
    ArenaParticipationError,
    GameParticipation,
    LocalAgentRegistration,
    PostgresArenaParticipationRepository,
)
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
        self.local_agent_values: dict[str, str] | None = None
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

    async def register_local_agent(
        self,
        **values: str,
    ) -> LocalAgentRegistration:
        self.local_agent_values = values
        if self.error is not None:
            raise ArenaParticipationError(self.error)
        return LocalAgentRegistration(
            agent_id="agent-local-1",
            display_name=values["display_name"],
            runtime_binding_id="rbind-local-1",
            connector_binding_id=values["connector_binding_id"],
            connector_binding_epoch=7,
            route_status="ready",
        )


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


def test_register_local_agent_freezes_owned_connector_route() -> None:
    repository = _Repository()
    raw_key = "local-agent-request-0001"

    response = _client(repository).post(
        "/api/local-agents",
        headers={"Idempotency-Key": raw_key},
        json={
            "connectorBindingId": "binding-1",
            "displayName": "My Codex",
        },
    )

    assert response.status_code == 201
    assert response.json() == {
        "agentId": "agent-local-1",
        "displayName": "My Codex",
        "runtimeBindingId": "rbind-local-1",
        "runtimeKind": "connector",
        "connectorBindingId": "binding-1",
        "connectorBindingEpoch": 7,
        "routeStatus": "ready",
        "schemaVersion": "arena.local-agent.v1",
    }
    assert repository.local_agent_values is not None
    assert repository.local_agent_values["owner_user_id"] == "user-1"
    assert repository.local_agent_values["connector_binding_id"] == "binding-1"
    assert repository.local_agent_values["key_digest"].startswith("sha256:")
    assert raw_key not in repr(repository.local_agent_values)


def test_connector_join_snapshot_freezes_binding_epoch_without_session() -> None:
    snapshot = PostgresArenaParticipationRepository._runtime_snapshot(
        {
            "runtime_kind": "connector",
            "connector_binding_id": "binding-1",
            "connector_binding_epoch": 7,
        }
    )

    assert snapshot == {
        "runtime_kind": "connector",
        "credential_id": None,
        "connector_binding_id": "binding-1",
        "connector_binding_epoch": 7,
    }
    assert "session_id" not in snapshot


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
