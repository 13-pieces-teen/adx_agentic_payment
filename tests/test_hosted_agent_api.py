"""HTTP security contracts for the fail-closed Hosted Agent API boundary."""

from __future__ import annotations

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from connector_gateway.config import ConnectorGatewayConfig
from connector_gateway.production import build_production_connector
from connector_gateway.repository import MemoryConnectorRepository
from hosted_agent_control_plane import (
    CapabilityCatalogService,
    CredentialStatus,
    CredentialIngressService,
    HostedAgentService,
    HostedProvisioningStatus,
    MemoryHostedAgentControlRepository,
)
from hosted_agent_runtime import (
    CapabilityRegistry,
    MemorySecretStore,
    ModelCapability,
    ThinkingMode,
)
from web.hosted_agent_api import create_hosted_agent_router
from web.api import create_app


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _capability() -> ModelCapability:
    return ModelCapability(
        provider_id="test-provider",
        adapter_id="test-structured",
        model_id="test-model-2026-07-24",
        display_name="Test structured model",
        supports_structured_output=True,
        thinking_mode=ThinkingMode.OPTIONAL,
        thinking_parameter_name="thinking.enabled",
        max_output_tokens=4096,
        request_timeout_cap_ms=60_000,
        adapter_version="test-adapter-v1",
        immutable_model_id=True,
        verified=True,
        enabled=True,
    )


def _test_app(invite: str = "hosted-api-invite-that-is-long-enough"):
    identity_repository = MemoryConnectorRepository()
    connector = build_production_connector(
        ConnectorGatewayConfig(
            database_url="postgresql://unused-in-memory",
            session_secret=(
                "hosted-api-session-secret-that-is-long-enough"
            ),
            public_app_url="https://arena.example.test",
            bootstrap_invite_hash=_digest(invite),
        ),
        identity_repository,
    )
    control_repository = (
        MemoryHostedAgentControlRepository.for_testing()
    )
    registry = CapabilityRegistry([_capability()])
    secret_store = MemorySecretStore.for_testing()
    credentials = CredentialIngressService(
        control_repository,
        secret_writer=secret_store.ports.writer,
        fingerprint_pepper=b"p" * 32,
        fingerprint_pepper_version=1,
        allow_non_durable_repository_for_tests=True,
    )
    agents = HostedAgentService(
        control_repository,
        capabilities=registry,
        hosted_agents_enabled=True,
        allow_non_durable_repository_for_tests=True,
    )
    catalog = CapabilityCatalogService(
        registry,
        hosted_agents_enabled=True,
        credential_ingress_configured=True,
    )
    app = FastAPI()
    app.state.hosted_control_repository = control_repository
    app.include_router(connector.router)
    app.include_router(
        create_hosted_agent_router(
            catalog=catalog,
            auth=connector.auth,
            credential_service=credentials,
            agent_service=agents,
            enable_mutations=True,
        )
    )
    return (
        TestClient(app, base_url="https://arena.example.test"),
        identity_repository,
    )


def _register(
    client: TestClient,
    *,
    invite: str,
    username: str,
) -> str:
    response = client.post(
        "/api/auth/register",
        json={
            "invite_code": invite,
            "username": username,
            "password": f"{username}-has-a-long-safe-password",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["csrf_token"]


def test_capabilities_have_explicit_creation_gate_and_safe_model_fields() -> None:
    client, _ = _test_app()
    response = client.get("/api/hosted-agents/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert body["creationEnabled"] is True
    assert body["reasonCodes"] == []
    assert body["registryVersion"] == "arena.provider-registry.v1"
    assert len(body["models"]) == 1
    model = body["models"][0]
    assert model["providerId"] == "test-provider"
    assert model["thinkingMode"] == "optional"
    assert model["supportsStructuredOutput"] is True
    assert "adapterId" not in model
    assert "thinkingParameterName" not in model
    assert "endpoint" not in model


def test_owner_routes_require_session_and_mutations_require_csrf() -> None:
    invite = "hosted-api-auth-invite-that-is-long-enough"
    client, _ = _test_app(invite)

    assert client.get("/api/hosted-agents?scope=mine").status_code == 401
    assert client.get("/api/model-credentials?scope=mine").status_code == 401
    anonymous = client.post(
        "/api/model-credentials",
        json={"providerId": "test-provider", "apiKey": "anonymous-key"},
        headers={"Idempotency-Key": "credential-anonymous-1"},
    )
    assert anonymous.status_code == 401

    _register(client, invite=invite, username="hosted-owner")
    missing_csrf = client.post(
        "/api/model-credentials",
        json={"providerId": "test-provider", "apiKey": "missing-csrf-key"},
        headers={"Idempotency-Key": "credential-missing-csrf"},
    )
    assert missing_csrf.status_code == 403


def test_credential_validation_never_echoes_rejected_secret_input() -> None:
    invite = "hosted-api-secret-invite-that-is-long-enough"
    client, _ = _test_app(invite)
    csrf = _register(client, invite=invite, username="secret-owner")
    sentinel = "sk-SENTINEL-NEVER-ECHO-123456"

    response = client.post(
        "/api/model-credentials",
        json={
            "providerId": "test-provider",
            "apiKey": {"invalid": sentinel},
        },
        headers={
            "Idempotency-Key": "credential-invalid-secret",
            "X-CSRF-Token": csrf,
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "invalid_request"}}
    assert sentinel not in response.text


def test_credential_body_rejects_duplicates_and_oversize_without_echo() -> None:
    invite = "hosted-api-bounded-body-invite-that-is-long-enough"
    client, _ = _test_app(invite)
    csrf = _register(client, invite=invite, username="bounded-body-owner")
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": "credential-bounded-body",
        "X-CSRF-Token": csrf,
    }
    sentinel = "duplicate-secret-sentinel"

    duplicate = client.post(
        "/api/model-credentials",
        content=(
            '{"providerId":"test-provider","apiKey":"first",'
            f'"apiKey":"{sentinel}"}}'
        ),
        headers=headers,
    )
    assert duplicate.status_code == 422
    assert duplicate.json() == {"detail": {"code": "invalid_request"}}
    assert sentinel not in duplicate.text

    oversized = client.post(
        "/api/model-credentials",
        content=b"x" * (70 * 1024 + 1),
        headers=headers,
    )
    assert oversized.status_code == 422
    assert oversized.json() == {"detail": {"code": "invalid_request"}}


def test_owner_scope_validation_never_echoes_rejected_query() -> None:
    invite = "hosted-api-query-safety-invite-that-is-long-enough"
    client, _ = _test_app(invite)
    _register(client, invite=invite, username="query-safety-owner")
    sentinel = "sk-SENTINEL-QUERY-ECHO-123456789"

    for path in (
        "/api/model-credentials",
        "/api/hosted-agents",
    ):
        response = client.get(path, params={"scope": sentinel})
        assert response.status_code == 422
        assert response.json() == {
            "detail": {"code": "invalid_request"}
        }
        assert sentinel not in response.text


def test_credential_and_agent_create_replay_without_secret_exposure() -> None:
    invite = "hosted-api-create-invite-that-is-long-enough"
    client, _ = _test_app(invite)
    csrf = _register(client, invite=invite, username="create-owner")
    provider_key = "test-only-provider-key-never-return"
    credential_headers = {
        "Idempotency-Key": "credential-create-stable-1",
        "X-CSRF-Token": csrf,
    }
    credential_body = {
        "providerId": "test-provider",
        "apiKey": provider_key,
    }

    created = client.post(
        "/api/model-credentials",
        json=credential_body,
        headers=credential_headers,
    )
    replay = client.post(
        "/api/model-credentials",
        json=credential_body,
        headers=credential_headers,
    )
    assert created.status_code == replay.status_code == 201
    assert created.json() == replay.json()
    credential_id = created.json()["credentialId"]
    assert created.json()["status"] == "stored"
    assert provider_key not in created.text
    assert "secretRef" not in created.text

    conflict = client.post(
        "/api/model-credentials",
        json={
            "providerId": "test-provider",
            "apiKey": "different-test-provider-key",
        },
        headers=credential_headers,
    )
    assert conflict.status_code == 409

    agent_headers = {
        "Idempotency-Key": "hosted-agent-create-stable-1",
        "X-CSRF-Token": csrf,
    }
    agent_body = {
        "displayName": "Hosted trader",
        "credentialId": credential_id,
        "providerId": "test-provider",
        "modelId": "test-model-2026-07-24",
        "thinkingEnabled": True,
        "strategyInstructions": "Preserve cash during volatility.",
    }
    agent = client.post(
        "/api/hosted-agents",
        json=agent_body,
        headers=agent_headers,
    )
    agent_replay = client.post(
        "/api/hosted-agents",
        json=agent_body,
        headers=agent_headers,
    )
    assert agent.status_code == agent_replay.status_code == 201
    assert agent.json() == agent_replay.json()
    assert agent.json()["provisioningStatus"] == "provisioning"
    assert agent.json()["routeStatus"] == "provisioning"
    assert provider_key not in agent.text
    assert "secretRef" not in agent.text

    listed = client.get("/api/hosted-agents?scope=mine")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert "strategyInstructions" not in listed.text


def test_owner_can_patch_ready_agent_strategy_without_resending_key() -> None:
    invite = "hosted-api-update-invite-that-is-long-enough"
    client, _ = _test_app(invite)
    csrf = _register(client, invite=invite, username="update-owner")
    credential = client.post(
        "/api/model-credentials",
        json={
            "providerId": "test-provider",
            "apiKey": "update-owner-test-key",
        },
        headers={
            "Idempotency-Key": "update-owner-credential",
            "X-CSRF-Token": csrf,
        },
    )
    created = client.post(
        "/api/hosted-agents",
        json={
            "displayName": "Updatable hosted trader",
            "credentialId": credential.json()["credentialId"],
            "providerId": "test-provider",
            "modelId": "test-model-2026-07-24",
            "thinkingEnabled": True,
            "strategyInstructions": "",
        },
        headers={
            "Idempotency-Key": "update-owner-agent",
            "X-CSRF-Token": csrf,
        },
    )
    repository = client.app.state.hosted_control_repository
    agent_id = created.json()["agentId"]
    credential_id = credential.json()["credentialId"]
    repository._agents[agent_id] = repository._agents[agent_id].model_copy(
        update={
            "provisioning_status": HostedProvisioningStatus.READY,
            "route_status": HostedProvisioningStatus.READY,
        }
    )
    repository._credentials[credential_id] = repository._credentials[
        credential_id
    ].model_copy(update={"status": CredentialStatus.VALID})

    headers = {
        "Idempotency-Key": "update-owner-strategy-1",
        "X-CSRF-Token": csrf,
    }
    body = {
        "providerId": "test-provider",
        "modelId": "test-model-2026-07-24",
        "thinkingEnabled": False,
        "strategyInstructions": (
            "Buy iron. Propose 7.000000 and accept at or below it."
        ),
    }
    updated = client.patch(
        f"/api/hosted-agents/{agent_id}",
        json=body,
        headers=headers,
    )
    replay = client.patch(
        f"/api/hosted-agents/{agent_id}",
        json=body,
        headers=headers,
    )

    assert updated.status_code == replay.status_code == 202
    assert updated.json() == replay.json()
    assert updated.json()["provisioningStatus"] == "provisioning"
    assert updated.json()["routeStatus"] == "provisioning"
    assert updated.json()["strategyInstructions"] == body[
        "strategyInstructions"
    ]
    assert "apiKey" not in updated.text
    assert "credentialId" in updated.json()


def test_cross_owner_agent_detail_is_indistinguishable_from_absence() -> None:
    first_invite = "hosted-api-first-owner-invite-long-enough"
    second_invite = "hosted-api-second-owner-invite-long-enough"
    client, identities = _test_app(first_invite)
    first_csrf = _register(
        client,
        invite=first_invite,
        username="first-hosted-owner",
    )
    credential = client.post(
        "/api/model-credentials",
        json={
            "providerId": "test-provider",
            "apiKey": "first-owner-test-key",
        },
        headers={
            "Idempotency-Key": "first-owner-credential",
            "X-CSRF-Token": first_csrf,
        },
    )
    agent = client.post(
        "/api/hosted-agents",
        json={
            "displayName": "Private hosted trader",
            "credentialId": credential.json()["credentialId"],
            "providerId": "test-provider",
            "modelId": "test-model-2026-07-24",
            "thinkingEnabled": True,
            "strategyInstructions": "Private owner strategy.",
        },
        headers={
            "Idempotency-Key": "first-owner-hosted-agent",
            "X-CSRF-Token": first_csrf,
        },
    )
    agent_id = agent.json()["agentId"]

    identities.invites[_digest(second_invite)] = {
        "token_hash": _digest(second_invite),
        "expires_at": None,
        "consumed_at": None,
        "consumed_by": None,
    }
    client.cookies.clear()
    _register(
        client,
        invite=second_invite,
        username="second-hosted-owner",
    )

    response = client.get(f"/api/hosted-agents/{agent_id}")
    missing = client.get("/api/hosted-agents/agent_does_not_exist")
    assert response.status_code == missing.status_code == 404
    assert response.json() == missing.json()


def test_disabled_catalog_mounts_no_mutation_dependency() -> None:
    catalog = CapabilityCatalogService(
        CapabilityRegistry(),
        hosted_agents_enabled=False,
        credential_ingress_configured=False,
    )
    app = FastAPI()
    app.include_router(create_hosted_agent_router(catalog=catalog))
    client = TestClient(app)

    capabilities = client.get("/api/hosted-agents/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["creationEnabled"] is False
    assert set(capabilities.json()["reasonCodes"]) == {
        "credential_ingress_unavailable",
        "hosted_agents_disabled",
        "no_enabled_models",
    }
    assert client.post("/api/model-credentials", json={}).status_code == 404


def test_owner_lists_map_repository_failures_to_safe_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_list(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("database-driver-sentinel")

    monkeypatch.setattr(
        MemoryHostedAgentControlRepository,
        "list_credentials_for_owner",
        fail_list,
    )
    monkeypatch.setattr(
        MemoryHostedAgentControlRepository,
        "list_hosted_agents_for_owner",
        fail_list,
    )
    invite = "hosted-api-list-failure-invite-long-enough"
    client, _ = _test_app(invite)
    _register(client, invite=invite, username="list-failure-owner")

    for path in (
        "/api/model-credentials?scope=mine",
        "/api/hosted-agents?scope=mine",
    ):
        response = client.get(path)
        assert response.status_code == 503
        assert response.json() == {
            "detail": {"code": "repository_unavailable"}
        }
        assert "database-driver-sentinel" not in response.text


def test_main_app_exposes_disabled_readiness_and_flag_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADX_HOSTED_AGENTS_ENABLED", raising=False)
    app = create_app(connector_demo_enabled=False)
    client = TestClient(app)

    response = client.get("/api/hosted-agents/capabilities")
    assert response.status_code == 200
    assert response.json()["creationEnabled"] is False
    assert response.json()["models"] == []

    preflight = client.options(
        "/api/model-credentials",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "content-type,idempotency-key,x-csrf-token"
            ),
        },
    )
    assert preflight.status_code == 200
    allowed_headers = preflight.headers[
        "access-control-allow-headers"
    ].casefold()
    assert "idempotency-key" in allowed_headers
    assert "x-csrf-token" in allowed_headers

    monkeypatch.setenv("ADX_HOSTED_AGENTS_ENABLED", "true")
    with pytest.raises(RuntimeError, match="Hosted Agent creation"):
        create_app(connector_demo_enabled=False)
