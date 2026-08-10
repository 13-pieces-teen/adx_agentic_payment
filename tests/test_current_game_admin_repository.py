from __future__ import annotations

import asyncio
import json

import pytest

from arena_game.postgres import (
    PawnhouseRepositoryError,
    PostgresPawnhouseRepository,
)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Acquire:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(
        self,
        *,
        has_participant_history: bool = False,
        prior_target: int | None = None,
    ) -> None:
        self.has_participant_history = has_participant_history
        self.prior_target = prior_target
        self.queries: list[str] = []
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self):
        return _Transaction()

    async def fetchval(self, query: str, *_: object):
        self.queries.append(query)
        if "SELECT EXISTS" in query:
            return self.has_participant_history
        return "game-current"

    async def fetchrow(self, query: str, *_: object):
        self.queries.append(query)
        if "FROM arena402.game_events" in query:
            if self.prior_target is None:
                return None
            return {
                "public_payload": {
                    "targetAgentCount": self.prior_target,
                }
            }
        return {
            "phase": "registration",
            "config_snapshot": {
                "minParticipants": 10,
                "maxParticipants": 100,
                "officialFillAfterSeconds": 300,
            },
        }

    async def execute(self, query: str, *arguments: object):
        self.queries.append(query)
        self.execute_calls.append((query, arguments))
        return "OK"


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


class _ConfigurationPool:
    async def fetchrow(self, _: str):
        return {
            "game_id": "game-current",
            "phase": "registration",
            "config_snapshot": {
                "officialFillAfterSeconds": 0,
            },
            "start_threshold": 32,
            "max_participants": 32,
            "has_participant_history": False,
        }


def test_repository_reports_exact_immediate_fill_configuration() -> None:
    repository = PostgresPawnhouseRepository(
        "",
        pool=_ConfigurationPool(),  # type: ignore[arg-type]
    )

    value = asyncio.run(
        repository.current_game_matchmaking_configuration()
    )

    assert value == {
        "gameId": "game-current",
        "targetAgentCount": 32,
        "maxParticipants": 32,
        "minimumTargetAgentCount": 10,
        "maximumTargetAgentCount": 100,
        "fillPolicy": "immediate_on_first_player_ready",
        "fillDelaySeconds": 0,
        "configurationEditable": True,
        "lockedReason": None,
    }


def test_repository_atomically_updates_target_capacity_and_private_audit() -> None:
    connection = _Connection()
    repository = PostgresPawnhouseRepository(
        "",
        pool=_Pool(connection),  # type: ignore[arg-type]
    )

    result = asyncio.run(
        repository.configure_current_game_matchmaking(
            expected_game_id="game-current",
            target_agent_count=32,
            actor_user_id="user-admin",
            request_digest="sha256:admin-request",
        )
    )

    assert result["targetAgentCount"] == 32
    game_lock_index = next(
        index
        for index, query in enumerate(connection.queries)
        if "FROM arena402.games" in query and "FOR UPDATE" in query
    )
    pointer_lock_index = next(
        index
        for index, query in enumerate(connection.queries)
        if "FROM arena402.current_game" in query and "FOR UPDATE" in query
    )
    assert game_lock_index < pointer_lock_index

    game_update = next(
        call
        for call in connection.execute_calls
        if "UPDATE arena402.games" in call[0]
    )
    assert game_update[1][0:2] == ("game-current", 32)
    frozen_config = json.loads(str(game_update[1][2]))
    assert frozen_config["minParticipants"] == 32
    assert frozen_config["maxParticipants"] == 32
    assert frozen_config["officialFillAfterSeconds"] == 0
    assert (
        frozen_config["officialFillPolicy"]
        == "immediate_on_first_player_ready"
    )

    audit_insert = next(
        call
        for call in connection.execute_calls
        if "INSERT INTO arena402.game_events" in call[0]
    )
    assert audit_insert[1][1] == "game.matchmaking_configured"
    assert json.loads(str(audit_insert[1][2])) == {
        "fillPolicy": "immediate_on_first_player_ready",
        "targetAgentCount": 32,
    }
    assert json.loads(str(audit_insert[1][3])) == {
        "actorUserId": "user-admin"
    }
    assert audit_insert[1][4] == "sha256:admin-request"


def test_repository_locks_configuration_after_first_participant_record() -> None:
    connection = _Connection(has_participant_history=True)
    repository = PostgresPawnhouseRepository(
        "",
        pool=_Pool(connection),  # type: ignore[arg-type]
    )

    with pytest.raises(
        PawnhouseRepositoryError,
        match="current_game_configuration_locked",
    ):
        asyncio.run(
            repository.configure_current_game_matchmaking(
                expected_game_id="game-current",
                target_agent_count=32,
                actor_user_id="user-admin",
                request_digest="sha256:admin-request",
            )
        )

    assert connection.execute_calls == []


def test_repository_reuses_a_completed_admin_request_without_new_audit() -> None:
    connection = _Connection(prior_target=32)
    repository = PostgresPawnhouseRepository(
        "",
        pool=_Pool(connection),  # type: ignore[arg-type]
    )

    result = asyncio.run(
        repository.configure_current_game_matchmaking(
            expected_game_id="game-current",
            target_agent_count=32,
            actor_user_id="user-admin",
            request_digest="sha256:admin-request",
        )
    )

    assert result["targetAgentCount"] == 32
    assert connection.execute_calls == []


def test_repository_rejects_reusing_request_key_for_another_target() -> None:
    connection = _Connection(prior_target=32)
    repository = PostgresPawnhouseRepository(
        "",
        pool=_Pool(connection),  # type: ignore[arg-type]
    )

    with pytest.raises(
        PawnhouseRepositoryError,
        match="idempotency_key_reused",
    ):
        asyncio.run(
            repository.configure_current_game_matchmaking(
                expected_game_id="game-current",
                target_agent_count=40,
                actor_user_id="user-admin",
                request_digest="sha256:admin-request",
            )
        )

    assert connection.execute_calls == []
