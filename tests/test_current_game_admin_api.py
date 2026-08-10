from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena_core.hashing import sha256_text_identifier
from arena_game.postgres import PawnhouseRepositoryError
from connector_gateway.auth import AuthPrincipal
from web.current_game_admin_api import create_current_game_admin_router


class _Auth:
    def __init__(self, provider_subject: str = "123456") -> None:
        self.provider_subject = provider_subject
        self.csrf_checked = False

    async def authenticate(self, _: object) -> AuthPrincipal:
        return AuthPrincipal(
            user_id="user-admin",
            username="admin",
            temporary=False,
            session_token_hash="s" * 64,
            csrf_hash="c" * 64,
            identity_provider="github",
            provider_subject=self.provider_subject,
        )

    async def require_csrf(self, _: object, __: object) -> None:
        self.csrf_checked = True


class _Repository:
    def __init__(self) -> None:
        self.target_agent_count = 10
        self.configured: dict[str, object] | None = None
        self.locked = False

    async def current_game(self, *, owner_user_id: str) -> dict[str, object]:
        assert owner_user_id == "user-admin"
        return {
            "game": {
                "gameId": "game-current",
                "status": "WAITING",
                "readyCount": 0,
                "startThreshold": self.target_agent_count,
                "maxParticipants": self.target_agent_count,
                "participants": [],
                "matchmaking": {
                    "targetSeats": self.target_agent_count,
                    "humanReadyCount": 0,
                    "officialReadyCount": 0,
                    "firstHumanReadyAt": None,
                    "fillAt": None,
                    "fillStatus": "IDLE",
                    "fillPolicy": "immediate_on_first_player_ready",
                    "fillDelaySeconds": 0,
                    "serverTime": "2026-08-10T00:00:00+00:00",
                },
            }
        }

    async def current_game_matchmaking_configuration(
        self,
    ) -> dict[str, object]:
        return {
            "gameId": "game-current",
            "targetAgentCount": self.target_agent_count,
            "maxParticipants": self.target_agent_count,
            "minimumTargetAgentCount": 10,
            "maximumTargetAgentCount": 100,
            "fillPolicy": "immediate_on_first_player_ready",
            "fillDelaySeconds": 0,
            "configurationEditable": not self.locked,
            "lockedReason": (
                "participant_history_exists" if self.locked else None
            ),
        }

    async def configure_current_game_matchmaking(
        self,
        **values: object,
    ) -> dict[str, object]:
        if self.locked:
            raise PawnhouseRepositoryError(
                "current_game_configuration_locked"
            )
        self.configured = values
        self.target_agent_count = int(values["target_agent_count"])
        return {
            "gameId": values["expected_game_id"],
            "targetAgentCount": self.target_agent_count,
        }


def _client(
    *,
    provider_subject: str = "123456",
    repository: _Repository | None = None,
) -> tuple[TestClient, _Auth, _Repository]:
    auth = _Auth(provider_subject)
    resolved_repository = repository or _Repository()
    app = FastAPI()
    app.include_router(
        create_current_game_admin_router(
            auth=auth,  # type: ignore[arg-type]
            repository=resolved_repository,  # type: ignore[arg-type]
            github_subjects=frozenset({"123456"}),
        )
    )
    return TestClient(app), auth, resolved_repository


def test_current_game_admin_snapshot_requires_allowlisted_github_subject() -> None:
    denied, _, _ = _client(provider_subject="999999")
    allowed, _, _ = _client()

    assert denied.get("/api/v1/admin/current-game").status_code == 403
    response = allowed.get("/api/v1/admin/current-game")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["matchmakingConfiguration"] == {
        "gameId": "game-current",
        "targetAgentCount": 10,
        "maxParticipants": 10,
        "minimumTargetAgentCount": 10,
        "maximumTargetAgentCount": 100,
        "fillPolicy": "immediate_on_first_player_ready",
        "fillDelaySeconds": 0,
        "configurationEditable": True,
        "lockedReason": None,
    }


def test_admin_can_freeze_an_exact_target_for_the_empty_current_game() -> None:
    client, auth, repository = _client()

    response = client.put(
        "/api/v1/admin/current-game/matchmaking",
        json={"gameId": "game-current", "targetAgentCount": 32},
        headers={"Idempotency-Key": "current-game-target:test-32"},
    )

    assert response.status_code == 200
    assert auth.csrf_checked is True
    assert repository.configured == {
        "expected_game_id": "game-current",
        "target_agent_count": 32,
        "actor_user_id": "user-admin",
        "request_digest": sha256_text_identifier(
            "current-game-target:test-32"
        ),
    }
    body = response.json()
    assert body["game"]["startThreshold"] == 32
    assert body["game"]["maxParticipants"] == 32
    assert body["matchmakingConfiguration"]["targetAgentCount"] == 32
    assert body["schemaVersion"] == "arena.current-game-admin.v1"


def test_admin_target_rejects_values_outside_ten_to_one_hundred() -> None:
    client, _, _ = _client()

    response = client.put(
        "/api/v1/admin/current-game/matchmaking",
        json={"gameId": "game-current", "targetAgentCount": 9},
        headers={"Idempotency-Key": "current-game-target:test-9"},
    )

    assert response.status_code == 422


def test_admin_target_is_locked_after_any_participant_history_exists() -> None:
    repository = _Repository()
    repository.locked = True
    client, _, _ = _client(repository=repository)

    response = client.put(
        "/api/v1/admin/current-game/matchmaking",
        json={"gameId": "game-current", "targetAgentCount": 32},
        headers={"Idempotency-Key": "current-game-target:test-locked"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {"code": "current_game_configuration_locked"}
    }


def test_admin_target_requires_a_valid_idempotency_key() -> None:
    client, _, repository = _client()

    response = client.put(
        "/api/v1/admin/current-game/matchmaking",
        json={"gameId": "game-current", "targetAgentCount": 32},
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": {"code": "invalid_idempotency_key"}
    }
    assert repository.configured is None
