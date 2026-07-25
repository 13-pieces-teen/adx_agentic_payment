from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

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
    ) -> dict[str, object]:
        assert agent_id is None
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
