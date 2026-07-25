from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena_core.connector_registration import PostgresConnectorArenaRegistrar
from connector_gateway.api import create_connector_router
from connector_gateway.auth import AuthPrincipal


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


class _Service:
    async def get_device(self, device_id: str) -> dict[str, object]:
        return {"device_id": device_id, "owner_id": "user-1"}

    async def create_binding(
        self,
        device_id: str,
        runtime_id: str,
        agent_id: str | None,
        display_name: str | None,
        working_directory: str | None,
    ) -> dict[str, object]:
        assert agent_id is None
        assert working_directory is None
        return {
            "binding_id": "binding-1",
            "device_id": device_id,
            "runtime_id": runtime_id,
            "agent_id": "agent-1",
            "display_name": display_name or "Local Codex",
            "status": "available",
            "binding_epoch": 1,
        }


class _Registrar:
    def __init__(self) -> None:
        self.values: dict[str, str] | None = None

    async def register_connector_binding(
        self,
        *,
        owner_user_id: str,
        connector_binding_id: str,
    ) -> dict[str, str]:
        self.values = {
            "owner_user_id": owner_user_id,
            "connector_binding_id": connector_binding_id,
        }
        return {
            "agentId": "agent-1",
            "runtimeBindingId": "rbind-1",
            "runtimeKind": "connector",
            "routeStatus": "provisioning",
            "schemaVersion": "arena.connector-registration.v1",
        }


def test_binding_creation_registers_the_arena_agent_for_the_session_owner() -> None:
    registrar = _Registrar()
    app = FastAPI()
    app.include_router(
        create_connector_router(
            _Service(),  # type: ignore[arg-type]
            auth=_Auth(),  # type: ignore[arg-type]
            arena_registrar=registrar,  # type: ignore[arg-type]
        )
    )

    response = TestClient(app).post(
        "/api/connectors/devices/device-1/bindings",
        json={"runtime_id": "runtime-1", "display_name": "Local Codex"},
    )

    assert response.status_code == 201
    assert response.json()["agent_id"] == "agent-1"
    assert response.json()["arenaRegistration"]["runtimeBindingId"] == "rbind-1"
    assert registrar.values == {
        "owner_user_id": "user-1",
        "connector_binding_id": "binding-1",
    }


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchrow(self, query: str, *args: object):
        if "FROM connector_bindings AS cb" in query:
            return {
                "binding_id": "binding-1",
                "agent_id": "agent-1",
                "binding_status": "available",
                "binding_record": {
                    "binding_epoch": 1,
                    "display_name": "Local Codex",
                },
                "owner_id": "user-1",
                "revoked_at": None,
                "available": True,
                "runtime_record": {
                    "capabilities": ["session.start", "task.dispatch"],
                },
            }
        if "FROM arena_runtime_bindings" in query:
            return None
        raise AssertionError(query)

    async def fetchval(self, query: str, *args: object):
        if query.lstrip().startswith("SELECT owner_user_id"):
            return None
        if "INSERT INTO arena_agents" in query:
            return "user-1"
        raise AssertionError(query)

    async def execute(self, query: str, *args: object) -> str:
        self.executions.append((query, args))
        return "OK"


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


def test_task_dispatch_capability_activates_the_compatibility_route() -> None:
    connection = _Connection()
    registrar = PostgresConnectorArenaRegistrar(
        "",
        pool=_Pool(connection),
    )

    registration = asyncio.run(
        registrar.register_connector_binding(
            owner_user_id="user-1",
            connector_binding_id="binding-1",
        )
    )

    assert registration["routeStatus"] == "ready"
    route_insert = next(
        args
        for query, args in connection.executions
        if "INSERT INTO arena_runtime_bindings" in query
    )
    assert route_insert[-1] == "ready"
