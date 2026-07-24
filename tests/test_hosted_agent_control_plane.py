"""Phase 4 Hosted Agent control-plane domain/service tests."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
from datetime import datetime, timezone
from typing import Any, Coroutine, TypeVar

import pytest
from pydantic import ValidationError

from hosted_agent_control_plane import (
    CapabilityCatalogService,
    ControlRepositoryError,
    CredentialIngressRequest,
    CredentialIngressService,
    CredentialStatus,
    HostedAgentCreateRequest,
    HostedAgentService,
    HostedControlPlaneError,
    HostedProvisioningStatus,
    MemoryHostedAgentControlRepository,
)
from hosted_agent_control_plane.models import CredentialRecord
from hosted_agent_control_plane.repository import (
    HostedAgentControlRepository,
)
from hosted_agent_runtime import (
    CapabilityRegistry,
    ModelCapability,
    SecretReference,
    SecretStoreOperationError,
    SecretWrite,
    ThinkingMode,
)


_T = TypeVar("_T")
_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
_PEPPER = b"p" * 32


def _run(coroutine: Coroutine[Any, Any, _T]) -> _T:
    return asyncio.run(coroutine)


def _capability(
    *,
    provider_id: str = "provider-a",
    model_id: str = "model-2026-07-01",
    thinking_mode: ThinkingMode = ThinkingMode.OPTIONAL,
    thinking_parameter_name: str | None = "thinking.enabled",
    max_output_tokens: int = 8192,
) -> ModelCapability:
    return ModelCapability(
        provider_id=provider_id,
        adapter_id=f"{provider_id}-responses",
        model_id=model_id,
        display_name=f"{provider_id} safe model",
        supports_structured_output=True,
        thinking_mode=thinking_mode,
        thinking_parameter_name=thinking_parameter_name,
        max_output_tokens=max_output_tokens,
        request_timeout_cap_ms=90_000,
        adapter_version="adapter-v1",
        immutable_model_id=True,
        verified=True,
        enabled=True,
    )


class _Ids:
    def __init__(self) -> None:
        self._next = 0

    def __call__(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}_{self._next:04d}"


class _RecordingWriteOnlySecretWriter:
    """Test double with the same write-only surface as a production writer."""

    def __init__(self) -> None:
        self.create_calls = 0
        self.last_ref: SecretReference | None = None
        self.last_write: SecretWrite | None = None
        self.received_digest: str | None = None

    async def create(
        self,
        secret_ref: SecretReference,
        secret: SecretWrite,
    ) -> SecretReference:
        self.create_calls += 1
        self.last_ref = secret_ref
        self.last_write = secret
        self.received_digest = hashlib.sha256(
            secret._copy_bytes()
        ).hexdigest()
        return secret_ref


class _CollapsedSecretPorts(_RecordingWriteOnlySecretWriter):
    async def resolve_for_worker(self, secret_ref: SecretReference) -> object:
        return object()


class _WrongReferenceSecretWriter(_RecordingWriteOnlySecretWriter):
    async def create(
        self,
        secret_ref: SecretReference,
        secret: SecretWrite,
    ) -> SecretReference:
        self.create_calls += 1
        self.last_ref = secret_ref
        self.last_write = secret
        return SecretReference("arena402/hosted-model/wrong-reference")


class _FailingWriteOnlySecretWriter(_RecordingWriteOnlySecretWriter):
    async def create(
        self,
        secret_ref: SecretReference,
        secret: SecretWrite,
    ) -> SecretReference:
        self.create_calls += 1
        self.last_ref = secret_ref
        self.last_write = secret
        raise RuntimeError("backend detail must be redacted")


def _credential_service(
    repository: MemoryHostedAgentControlRepository,
    *,
    writer: _RecordingWriteOnlySecretWriter | None = None,
    ids: _Ids | None = None,
) -> tuple[CredentialIngressService, _RecordingWriteOnlySecretWriter]:
    selected_writer = writer or _RecordingWriteOnlySecretWriter()
    return (
        CredentialIngressService(
            repository,
            secret_writer=selected_writer,
            fingerprint_pepper=_PEPPER,
            fingerprint_pepper_version=1,
            allow_non_durable_repository_for_tests=True,
            id_factory=ids or _Ids(),
            clock=lambda: _NOW,
        ),
        selected_writer,
    )


def _create_credential(
    service: CredentialIngressService,
    *,
    owner: str = "user-a",
    provider_id: str = "provider-a",
    api_key: str = "provider-key-sensitive-123456",
    idempotency_key: str = "credential-request-0001",
):
    return _run(
        service.create_credential(
            owner_user_id=owner,
            request=CredentialIngressRequest(
                provider_id=provider_id,
                api_key=api_key,
                idempotency_key=idempotency_key,
            ),
        )
    )


def _agent_service(
    repository: MemoryHostedAgentControlRepository,
    *,
    registry: CapabilityRegistry | None = None,
    ids: _Ids | None = None,
) -> HostedAgentService:
    return HostedAgentService(
        repository,
        capabilities=registry or CapabilityRegistry([_capability()]),
        hosted_agents_enabled=True,
        allow_non_durable_repository_for_tests=True,
        id_factory=ids or _Ids(),
        clock=lambda: _NOW,
    )


def _agent_request(
    *,
    credential_id: str,
    provider_id: str = "provider-a",
    model_id: str = "model-2026-07-01",
    thinking_enabled: bool = True,
    display_name: str = "My Arena Agent",
    strategy_instructions: str = "Prefer conservative trades.",
    idempotency_key: str = "hosted-agent-request-0001",
) -> HostedAgentCreateRequest:
    return HostedAgentCreateRequest(
        display_name=display_name,
        credential_id=credential_id,
        provider_id=provider_id,
        model_id=model_id,
        thinking_enabled=thinking_enabled,
        strategy_instructions=strategy_instructions,
        idempotency_key=idempotency_key,
    )


def test_request_models_are_strict_and_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        HostedAgentCreateRequest.model_validate(
            {
                "displayName": "Agent",
                "credentialId": "cred_1",
                "providerId": "provider-a",
                "modelId": "model-2026-07-01",
                "thinkingEnabled": 1,
                "idempotencyKey": "agent-key-0001",
            }
        )

    with pytest.raises(ValidationError):
        CredentialIngressRequest.model_validate(
            {
                "providerId": "provider-a",
                "apiKey": "secret-value",
                "idempotencyKey": "credential-key-0001",
                "endpoint": "http://127.0.0.1",
            }
        )


def test_capability_list_and_readiness_are_safe_projections() -> None:
    registry = CapabilityRegistry([_capability()])
    service = CapabilityCatalogService(
        registry,
        hosted_agents_enabled=True,
        credential_ingress_configured=True,
    )

    capability = service.list_capabilities()[0]
    payload = capability.model_dump(mode="json", by_alias=True)
    assert payload["providerId"] == "provider-a"
    assert payload["thinkingCanToggle"] is True
    assert "adapterId" not in payload
    assert "thinkingParameterName" not in payload

    readiness = service.readiness()
    assert readiness.available is True
    assert readiness.enabled_model_count == 1
    assert readiness.reason_codes == ()

    unavailable = CapabilityCatalogService(
        CapabilityRegistry(),
        hosted_agents_enabled=False,
        credential_ingress_configured=False,
    ).readiness()
    assert unavailable.available is False
    assert unavailable.reason_codes == (
        "hosted_agents_disabled",
        "credential_ingress_unavailable",
        "no_enabled_models",
    )


def test_credential_ingress_requires_an_async_write_only_writer() -> None:
    repository = MemoryHostedAgentControlRepository.for_testing()
    with pytest.raises(HostedControlPlaneError) as exc:
        CredentialIngressService(
            repository,
            secret_writer=object(),  # type: ignore[arg-type]
            fingerprint_pepper=_PEPPER,
            fingerprint_pepper_version=1,
            allow_non_durable_repository_for_tests=True,
        )
    assert exc.value.code == "secret_writer_not_write_only"

    with pytest.raises(HostedControlPlaneError) as exc:
        CredentialIngressService(
            repository,
            secret_writer=_CollapsedSecretPorts(),
            fingerprint_pepper=_PEPPER,
            fingerprint_pepper_version=1,
            allow_non_durable_repository_for_tests=True,
        )
    assert exc.value.code == "secret_writer_not_write_only"


def test_credential_ingress_redacts_and_closes_raw_secret() -> None:
    raw_key = "provider-key-sensitive-123456"
    repository = MemoryHostedAgentControlRepository.for_testing()
    service, writer = _credential_service(repository)
    request = CredentialIngressRequest(
        provider_id="provider-a",
        api_key=raw_key,
        idempotency_key="credential-request-0001",
    )

    created = _run(
        service.create_credential(
            owner_user_id="user-a",
            request=request,
        )
    )

    assert created.status is CredentialStatus.STORED
    assert writer.create_calls == 1
    assert writer.received_digest == hashlib.sha256(
        raw_key.encode("utf-8")
    ).hexdigest()
    assert writer.last_write is not None
    with pytest.raises(SecretStoreOperationError) as exc:
        writer.last_write._copy_bytes()
    assert exc.value.code == "secret_value_closed"

    persisted = _run(
        repository.get_credential_for_owner(
            owner_user_id="user-a",
            credential_id=created.credential_id,
        )
    )
    assert persisted is not None
    expected_fingerprint = hmac.new(
        _PEPPER,
        raw_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert persisted.fingerprint == expected_fingerprint
    assert created.fingerprint_hint == (
        f"hmac-sha256:{expected_fingerprint[:12]}"
    )

    rendered = " ".join(
        (
            repr(request),
            request.model_dump_json(),
            repr(created),
            created.model_dump_json(),
            repr(persisted),
            persisted.model_dump_json(),
        )
    )
    assert raw_key not in rendered
    assert "secret_ref" not in created.model_dump()
    assert "fingerprint" not in created.model_dump()


def test_credential_idempotency_replays_and_conflicts_before_secret_write() -> None:
    repository = MemoryHostedAgentControlRepository.for_testing()
    service, writer = _credential_service(repository)

    first = _create_credential(service)
    replay = _create_credential(service)
    assert replay == first
    assert writer.create_calls == 1

    with pytest.raises(HostedControlPlaneError) as exc:
        _create_credential(
            service,
            api_key="different-provider-key-987654",
        )
    assert exc.value.code == "idempotency_conflict"
    assert writer.create_calls == 1


@pytest.mark.parametrize(
    ("provider_id", "idempotency_key", "expected"),
    [
        (
            "sk-abcdefghijklmnop",
            "credential-request-0001",
            "invalid_identifier",
        ),
        (
            "provider-a",
            "sk-abcdefghijklmnop",
            "invalid_idempotency_key",
        ),
    ],
)
def test_credential_auxiliary_fields_cannot_persist_secret_material(
    provider_id: str,
    idempotency_key: str,
    expected: str,
) -> None:
    repository = MemoryHostedAgentControlRepository.for_testing()
    service, writer = _credential_service(repository)
    with pytest.raises(HostedControlPlaneError) as exc:
        _create_credential(
            service,
            provider_id=provider_id,
            idempotency_key=idempotency_key,
        )
    assert exc.value.code == expected
    assert writer.create_calls == 0
    assert _run(
        repository.list_credentials_for_owner(owner_user_id="user-a")
    ) == ()


def test_repository_receives_only_idempotency_digest() -> None:
    raw_idempotency_key = "credential-request-raw-0001"
    repository = MemoryHostedAgentControlRepository.for_testing()
    service, _ = _credential_service(repository)
    _create_credential(
        service,
        idempotency_key=raw_idempotency_key,
    )

    idempotency_state = repr(repository._idempotency)
    assert raw_idempotency_key not in idempotency_state
    assert "sha256:" in idempotency_state


def test_pending_write_replay_requires_recovery_and_does_not_rewrite() -> None:
    repository = MemoryHostedAgentControlRepository.for_testing()
    writer = _FailingWriteOnlySecretWriter()
    service, _ = _credential_service(repository, writer=writer)

    with pytest.raises(HostedControlPlaneError) as first:
        _create_credential(service)
    assert first.value.code == "secret_store_unavailable"
    assert "backend detail" not in str(first.value)

    records = _run(
        repository.list_credentials_for_owner(owner_user_id="user-a")
    )
    assert len(records) == 1
    assert records[0].status is CredentialStatus.PENDING_WRITE

    with pytest.raises(HostedControlPlaneError) as replay:
        _create_credential(service)
    assert replay.value.code == "credential_write_recovery_required"
    assert writer.create_calls == 1


def test_wrong_secret_reference_remains_pending_for_recovery() -> None:
    repository = MemoryHostedAgentControlRepository.for_testing()
    writer = _WrongReferenceSecretWriter()
    service, _ = _credential_service(repository, writer=writer)

    with pytest.raises(HostedControlPlaneError) as exc:
        _create_credential(service)
    assert exc.value.code == "secret_store_unavailable"

    records = _run(
        repository.list_credentials_for_owner(owner_user_id="user-a")
    )
    assert len(records) == 1
    assert records[0].status is CredentialStatus.PENDING_WRITE


def test_hosted_agent_creation_is_atomic_provisioning_and_idempotent() -> None:
    repository = MemoryHostedAgentControlRepository.for_testing()
    credential_service, _ = _credential_service(repository)
    credential = _create_credential(credential_service)
    service = _agent_service(repository)
    request = _agent_request(credential_id=credential.credential_id)

    first = _run(
        service.create_hosted_agent(
            owner_user_id="user-a",
            request=request,
        )
    )
    assert first.provisioning_status is HostedProvisioningStatus.PROVISIONING
    assert first.route_status is HostedProvisioningStatus.PROVISIONING
    assert first.credential_id == credential.credential_id

    bound_credential = _run(
        repository.get_credential_for_owner(
            owner_user_id="user-a",
            credential_id=credential.credential_id,
        )
    )
    assert bound_credential is not None
    assert bound_credential.status is CredentialStatus.PENDING_VALIDATION

    # Credential-create replay intentionally returns the current owner-scoped
    # resource projection, not a stale original response snapshot.
    credential_replay = _create_credential(credential_service)
    assert credential_replay.status is CredentialStatus.PENDING_VALIDATION

    # Replay still succeeds after the transaction moved the credential out of
    # STORED; the repository checks idempotency before usability constraints.
    replay = _run(
        service.create_hosted_agent(
            owner_user_id="user-a",
            request=request,
        )
    )
    assert replay == first
    assert len(
        _run(service.list_hosted_agents(owner_user_id="user-a"))
    ) == 1

    with pytest.raises(HostedControlPlaneError) as exc:
        _run(
            service.create_hosted_agent(
                owner_user_id="user-a",
                request=_agent_request(
                    credential_id=credential.credential_id,
                    display_name="Different Agent",
                ),
            )
        )
    assert exc.value.code == "idempotency_conflict"


def test_completed_agent_replay_precedes_mutable_prerequisite_validation() -> None:
    repository = MemoryHostedAgentControlRepository.for_testing()
    credential_service, _ = _credential_service(repository)
    credential = _create_credential(credential_service)
    request = _agent_request(credential_id=credential.credential_id)

    first = _run(
        _agent_service(repository).create_hosted_agent(
            owner_user_id="user-a",
            request=request,
        )
    )

    # Model catalogues and credential lifecycle state can change after the
    # original transaction. An exact completed replay remains authoritative.
    repository._credentials.pop(credential.credential_id)
    replay_service = _agent_service(
        repository,
        registry=CapabilityRegistry(),
    )
    replay = _run(
        replay_service.create_hosted_agent(
            owner_user_id="user-a",
            request=request,
        )
    )
    assert replay == first

    # The same key cannot be redirected by changing request metadata, even
    # when the current model/credential prerequisites are unavailable.
    with pytest.raises(HostedControlPlaneError) as conflict:
        _run(
            replay_service.create_hosted_agent(
                owner_user_id="user-a",
                request=_agent_request(
                    credential_id=credential.credential_id,
                    display_name="Changed replay target",
                ),
            )
        )
    assert conflict.value.code == "idempotency_conflict"

    # Another owner cannot observe or reuse the completed replay.
    with pytest.raises(HostedControlPlaneError) as cross_owner:
        _run(
            replay_service.create_hosted_agent(
                owner_user_id="user-b",
                request=request,
            )
        )
    assert cross_owner.value.code == "credential_not_found"


def test_multiline_strategy_is_canonicalized_before_persistence() -> None:
    repository = MemoryHostedAgentControlRepository.for_testing()
    credential_service, _ = _credential_service(repository)
    credential = _create_credential(credential_service)
    service = _agent_service(repository)

    agent = _run(
        service.create_hosted_agent(
            owner_user_id="user-a",
            request=_agent_request(
                credential_id=credential.credential_id,
                strategy_instructions=(
                    "Preserve cash.\r\nTrade only with evidence.\tPass on risk."
                ),
            ),
        )
    )

    assert agent.strategy_instructions == (
        "Preserve cash. Trade only with evidence. Pass on risk."
    )


def test_display_name_cannot_smuggle_provider_key_into_agent_record() -> None:
    repository = MemoryHostedAgentControlRepository.for_testing()
    credential_service, _ = _credential_service(repository)
    credential = _create_credential(credential_service)
    service = _agent_service(repository)
    raw_key = "sk-abcdefghijklmnop"

    with pytest.raises(HostedControlPlaneError) as exc:
        _run(
            service.create_hosted_agent(
                owner_user_id="user-a",
                request=_agent_request(
                    credential_id=credential.credential_id,
                    display_name=raw_key,
                ),
            )
        )
    assert exc.value.code == "invalid_display_name"
    assert raw_key not in str(exc.value)
    assert _run(service.list_hosted_agents(owner_user_id="user-a")) == ()


def test_owner_scoped_list_and_detail_hide_cross_owner_objects() -> None:
    repository = MemoryHostedAgentControlRepository.for_testing()
    credential_service, _ = _credential_service(repository)
    credential = _create_credential(credential_service)
    agent_service = _agent_service(repository)
    agent = _run(
        agent_service.create_hosted_agent(
            owner_user_id="user-a",
            request=_agent_request(credential_id=credential.credential_id),
        )
    )

    assert _run(
        credential_service.list_credentials(owner_user_id="user-b")
    ) == ()
    with pytest.raises(HostedControlPlaneError) as exc:
        _run(
            credential_service.get_credential(
                owner_user_id="user-b",
                credential_id=credential.credential_id,
            )
        )
    assert exc.value.code == "credential_not_found"

    assert _run(agent_service.list_hosted_agents(owner_user_id="user-b")) == ()
    with pytest.raises(HostedControlPlaneError) as exc:
        _run(
            agent_service.get_hosted_agent(
                owner_user_id="user-b",
                agent_id=agent.agent_id,
            )
        )
    assert exc.value.code == "agent_not_found"

    detail_payload = _run(
        agent_service.get_hosted_agent(
            owner_user_id="user-a",
            agent_id=agent.agent_id,
        )
    ).model_dump(mode="json", by_alias=True)
    assert "secretRef" not in detail_payload
    assert "fingerprint" not in detail_payload
    assert "configHash" not in detail_payload
    assert "validationJobId" not in detail_payload


@pytest.mark.parametrize(
    ("registry", "model_id", "thinking_enabled", "expected"),
    [
        (
            CapabilityRegistry([_capability()]),
            "unknown-model-2026-01-01",
            False,
            "model_not_available",
        ),
        (
            CapabilityRegistry(
                [
                    _capability(
                        thinking_mode=ThinkingMode.UNSUPPORTED,
                        thinking_parameter_name=None,
                    )
                ]
            ),
            "model-2026-07-01",
            True,
            "thinking_not_supported",
        ),
    ],
)
def test_unsupported_capability_fails_closed(
    registry: CapabilityRegistry,
    model_id: str,
    thinking_enabled: bool,
    expected: str,
) -> None:
    repository = MemoryHostedAgentControlRepository.for_testing()
    credential_service, _ = _credential_service(repository)
    credential = _create_credential(credential_service)
    service = _agent_service(repository, registry=registry)

    with pytest.raises(HostedControlPlaneError) as exc:
        _run(
            service.create_hosted_agent(
                owner_user_id="user-a",
                request=_agent_request(
                    credential_id=credential.credential_id,
                    model_id=model_id,
                    thinking_enabled=thinking_enabled,
                ),
            )
        )
    assert exc.value.code == expected
    assert _run(service.list_hosted_agents(owner_user_id="user-a")) == ()


def test_provider_mismatch_is_rejected_before_agent_transaction() -> None:
    repository = MemoryHostedAgentControlRepository.for_testing()
    credential_service, _ = _credential_service(repository)
    credential = _create_credential(credential_service)
    service = _agent_service(
        repository,
        registry=CapabilityRegistry(
            [_capability(provider_id="provider-b")]
        ),
    )

    with pytest.raises(HostedControlPlaneError) as exc:
        _run(
            service.create_hosted_agent(
                owner_user_id="user-a",
                request=_agent_request(
                    credential_id=credential.credential_id,
                    provider_id="provider-b",
                ),
            )
        )
    assert exc.value.code == "provider_mismatch"


def test_memory_repository_is_explicitly_test_only_and_port_has_no_raw_key() -> None:
    with pytest.raises(ControlRepositoryError) as exc:
        MemoryHostedAgentControlRepository()
    assert exc.value.code == "memory_repository_not_explicitly_test_only"

    repository = MemoryHostedAgentControlRepository.for_testing()
    assert repository.durable is False
    with pytest.raises(AttributeError):
        repository.durable = True  # type: ignore[misc]
    assert not hasattr(repository, "api_key")
    assert not hasattr(repository, "secret_writer")
    assert "api_key" not in CredentialRecord.model_fields
    assert "raw_key" not in CredentialRecord.model_fields

    with pytest.raises(HostedControlPlaneError) as exc:
        CredentialIngressService(
            repository,
            secret_writer=_RecordingWriteOnlySecretWriter(),
            fingerprint_pepper=_PEPPER,
            fingerprint_pepper_version=1,
        )
    assert exc.value.code == "non_durable_repository_forbidden"

    with pytest.raises(HostedControlPlaneError) as exc:
        HostedAgentService(
            repository,
            capabilities=CapabilityRegistry([_capability()]),
            hosted_agents_enabled=True,
        )
    assert exc.value.code == "non_durable_repository_forbidden"

    for method_name in (
        "reserve_credential",
        "mark_credential_stored_and_complete_idempotency",
        "get_hosted_agent_creation_replay",
        "create_hosted_agent",
    ):
        parameters = inspect.signature(
            getattr(HostedAgentControlRepository, method_name)
        ).parameters
        assert "api_key" not in parameters
        assert "raw_key" not in parameters
        assert "secret_write" not in parameters
        if method_name in {
            "reserve_credential",
            "mark_credential_stored_and_complete_idempotency",
            "get_hosted_agent_creation_replay",
            "create_hosted_agent",
        }:
            assert "idempotency_key" not in parameters
            assert "idempotency_key_digest" in parameters


def test_error_codes_are_runtime_closed_and_do_not_echo_unknown_values() -> None:
    secret_like = "sk-this-must-never-be-an-error-code"
    with pytest.raises(ValueError) as control_error:
        HostedControlPlaneError(secret_like)  # type: ignore[arg-type]
    assert secret_like not in str(control_error.value)

    with pytest.raises(ValueError) as repository_error:
        ControlRepositoryError(secret_like)  # type: ignore[arg-type]
    assert secret_like not in str(repository_error.value)


def test_control_plane_caps_model_output_to_database_limit() -> None:
    repository = MemoryHostedAgentControlRepository.for_testing()
    credential_service, _ = _credential_service(repository)
    credential = _create_credential(credential_service)
    registry = CapabilityRegistry(
        [_capability(max_output_tokens=100_000)]
    )
    catalogue = CapabilityCatalogService(
        registry,
        hosted_agents_enabled=True,
        credential_ingress_configured=True,
    )
    assert catalogue.list_capabilities()[0].max_output_tokens == 65_536

    service = _agent_service(repository, registry=registry)
    agent = _run(
        service.create_hosted_agent(
            owner_user_id="user-a",
            request=_agent_request(credential_id=credential.credential_id),
        )
    )
    persisted = _run(
        repository.get_hosted_agent_for_owner(
            owner_user_id="user-a",
            agent_id=agent.agent_id,
        )
    )
    assert persisted is not None
    assert persisted.max_output_tokens == 65_536


def test_owner_contract_rejects_invalid_id_before_repository_access() -> None:
    repository = MemoryHostedAgentControlRepository.for_testing()
    credential_service, _ = _credential_service(repository)
    agent_service = _agent_service(repository)

    with pytest.raises(HostedControlPlaneError) as exc:
        _run(
            credential_service.list_credentials(
                owner_user_id="owner with spaces",
            )
        )
    assert exc.value.code == "invalid_owner"

    with pytest.raises(HostedControlPlaneError) as exc:
        _run(
            agent_service.list_hosted_agents(
                owner_user_id="owner with spaces",
            )
        )
    assert exc.value.code == "invalid_owner"

    with pytest.raises(HostedControlPlaneError) as exc:
        _run(
            credential_service.list_credentials(
                owner_user_id="u" * 129,
            )
        )
    assert exc.value.code == "invalid_owner"


class _BrokenDurableRepository:
    @property
    def durable(self) -> bool:
        return True

    async def reserve_credential(self, **_: object) -> object:
        raise RuntimeError("SQL and backend detail must not escape")

    async def get_credential_for_owner(self, **_: object) -> object:
        raise RuntimeError("SQL and backend detail must not escape")


def test_unknown_repository_errors_are_safely_collapsed() -> None:
    repository = _BrokenDurableRepository()
    credential_service = CredentialIngressService(
        repository,  # type: ignore[arg-type]
        secret_writer=_RecordingWriteOnlySecretWriter(),
        fingerprint_pepper=_PEPPER,
        fingerprint_pepper_version=1,
    )
    with pytest.raises(HostedControlPlaneError) as credential_error:
        _create_credential(credential_service)
    assert credential_error.value.code == "repository_unavailable"
    assert "SQL" not in str(credential_error.value)

    agent_service = HostedAgentService(
        repository,  # type: ignore[arg-type]
        capabilities=CapabilityRegistry([_capability()]),
        hosted_agents_enabled=True,
    )
    with pytest.raises(HostedControlPlaneError) as agent_error:
        _run(
            agent_service.create_hosted_agent(
                owner_user_id="user-a",
                request=_agent_request(credential_id="cred_0001"),
            )
        )
    assert agent_error.value.code == "repository_unavailable"
    assert "SQL" not in str(agent_error.value)


def test_memory_state_transitions_use_repository_clock() -> None:
    transition_time = datetime(2026, 7, 24, 12, 0, 1, tzinfo=timezone.utc)
    repository = MemoryHostedAgentControlRepository.for_testing(
        clock=lambda: transition_time
    )
    credential_service, _ = _credential_service(repository)
    credential = _create_credential(credential_service)
    assert credential.created_at == _NOW
    assert credential.updated_at == transition_time
