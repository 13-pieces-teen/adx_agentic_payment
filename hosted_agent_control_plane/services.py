"""Network-independent application services for Hosted Agent provisioning."""

from __future__ import annotations

import inspect
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Final, Literal, TypeAlias, cast

from arena_agent_contracts import AGENT_TASK_SCHEMA_VERSION_V1
from arena_core.hashing import sha256_identifier, sha256_text_identifier
from arena_core.ingress_security import (
    ArenaIngressSecurityError,
    validate_runtime_controlled_text,
)
from hosted_agent_runtime.capabilities import (
    CapabilityError,
    CapabilityRegistry,
)
from hosted_agent_runtime.runtime_contract import (
    AGENT_ACTION_SCHEMA_VERSION_V1,
    HOSTED_AGENT_INSTRUCTION_VERSION_V1,
    MAX_STRATEGY_BYTES,
)
from hosted_agent_runtime.secret_store import (
    SecretReference,
    SecretStoreError,
    SecretWrite,
    SecretWriter,
)

from .models import (
    AgentIdentityStatus,
    CapabilityProjection,
    CredentialIngressRequest,
    CredentialMetadata,
    CredentialRecord,
    CredentialStatus,
    HostedAgentCreateRequest,
    HostedAgentDetail,
    HostedAgentRecord,
    HostedAgentSummary,
    HostedAgentUpdateRequest,
    HostedProvisioningStatus,
    HostedReadinessProjection,
    ReservationDisposition,
)
from .repository import (
    ControlRepositoryError,
    HostedAgentControlRepository,
)


ControlPlaneErrorCode: TypeAlias = Literal[
    "credential_ingress_unavailable",
    "credential_not_found",
    "credential_not_usable",
    "credential_write_recovery_required",
    "agent_not_found",
    "agent_not_ready",
    "hosted_agents_disabled",
    "idempotency_conflict",
    "invalid_display_name",
    "invalid_fingerprint_pepper",
    "invalid_identifier",
    "invalid_idempotency_key",
    "invalid_owner",
    "non_durable_repository_forbidden",
    "invalid_secret_material",
    "invalid_strategy",
    "model_not_available",
    "provider_mismatch",
    "repository_unavailable",
    "secret_store_unavailable",
    "secret_writer_not_write_only",
    "thinking_always_on",
    "thinking_not_supported",
]

_MIN_PEPPER_BYTES: Final[int] = 32
_MAX_OWNER_ID_LENGTH: Final[int] = 128
_MAX_INPUT_BYTES: Final[int] = 64 * 1024
_MAX_CONTEXT_ITEMS: Final[int] = 256
_MAX_HOSTED_OUTPUT_TOKENS: Final[int] = 65_536
_CAPABILITY_RESOLUTION_BUDGET_MS: Final[int] = 1_800_000
_PERSISTED_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
)
_CONTROL_PLANE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "agent_not_found",
        "agent_not_ready",
        "credential_ingress_unavailable",
        "credential_not_found",
        "credential_not_usable",
        "credential_write_recovery_required",
        "hosted_agents_disabled",
        "idempotency_conflict",
        "invalid_display_name",
        "invalid_fingerprint_pepper",
        "invalid_identifier",
        "invalid_idempotency_key",
        "invalid_owner",
        "invalid_secret_material",
        "invalid_strategy",
        "model_not_available",
        "non_durable_repository_forbidden",
        "provider_mismatch",
        "repository_unavailable",
        "secret_store_unavailable",
        "secret_writer_not_write_only",
        "thinking_always_on",
        "thinking_not_supported",
    }
)


class HostedControlPlaneError(RuntimeError):
    """Safe service failure that never retains rejected values."""

    def __init__(self, code: ControlPlaneErrorCode) -> None:
        if type(code) is not str or code not in _CONTROL_PLANE_ERROR_CODES:
            raise ValueError("invalid hosted control-plane error code")
        self.code = cast(ControlPlaneErrorCode, code)
        super().__init__(f"Hosted control-plane operation failed ({code})")


def _safe_owner_id(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_OWNER_ID_LENGTH
        or not _PERSISTED_IDENTIFIER_PATTERN.fullmatch(value)
    ):
        raise HostedControlPlaneError("invalid_owner")
    try:
        validate_runtime_controlled_text(value, include_pii=False)
    except ArenaIngressSecurityError:
        raise HostedControlPlaneError("invalid_owner") from None
    return value


def _safe_persisted_identifier(
    value: str,
    *,
    error_code: Literal["invalid_identifier", "invalid_idempotency_key"],
) -> str:
    if type(value) is not str or not value or len(value) > 256:
        raise HostedControlPlaneError(error_code)
    try:
        validate_runtime_controlled_text(value, include_pii=False)
    except ArenaIngressSecurityError:
        raise HostedControlPlaneError(error_code) from None
    return value


def _safe_strategy(value: str) -> str:
    if (
        type(value) is not str
        or len(value.encode("utf-8")) > MAX_STRATEGY_BYTES
    ):
        raise HostedControlPlaneError("invalid_strategy")
    normalized = (
        value.replace("\r\n", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("\t", " ")
    )
    try:
        validate_runtime_controlled_text(normalized)
    except ArenaIngressSecurityError:
        raise HostedControlPlaneError("invalid_strategy") from None
    return normalized


def _safe_display_name(value: str) -> str:
    try:
        validate_runtime_controlled_text(value)
    except ArenaIngressSecurityError:
        raise HostedControlPlaneError("invalid_display_name") from None
    return value


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _credential_projection(record: CredentialRecord) -> CredentialMetadata:
    return CredentialMetadata(
        credential_id=record.credential_id,
        provider_id=record.provider_id,
        status=record.status,
        fingerprint_hint=f"hmac-sha256:{record.fingerprint[:12]}",
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _agent_summary(record: HostedAgentRecord) -> HostedAgentSummary:
    return HostedAgentSummary(
        agent_id=record.agent_id,
        display_name=record.display_name,
        provider_id=record.provider_id,
        model_id=record.model_id,
        thinking_enabled=record.thinking_enabled,
        provisioning_status=record.provisioning_status,
        route_status=record.route_status,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _agent_detail(record: HostedAgentRecord) -> HostedAgentDetail:
    return HostedAgentDetail(
        **_agent_summary(record).model_dump(),
        credential_id=record.credential_id,
        strategy_instructions=record.strategy_instructions,
    )


def _map_repository_error(exc: ControlRepositoryError) -> HostedControlPlaneError:
    mapping: dict[str, ControlPlaneErrorCode] = {
        "agent_not_found": "agent_not_found",
        "agent_not_ready": "agent_not_ready",
        "credential_not_found": "credential_not_found",
        "credential_not_usable": "credential_not_usable",
        "idempotency_conflict": "idempotency_conflict",
        "provider_mismatch": "provider_mismatch",
    }
    return HostedControlPlaneError(
        mapping.get(exc.code, "credential_not_usable")
    )


def _repository_unavailable() -> HostedControlPlaneError:
    return HostedControlPlaneError("repository_unavailable")


def _map_capability_error(exc: CapabilityError) -> HostedControlPlaneError:
    mapping: dict[str, ControlPlaneErrorCode] = {
        "model_not_available": "model_not_available",
        "thinking_always_on": "thinking_always_on",
        "thinking_not_supported": "thinking_not_supported",
    }
    return HostedControlPlaneError(
        mapping.get(exc.code, "model_not_available")
    )


def _is_write_only_secret_writer(candidate: object) -> bool:
    create = getattr(candidate, "create", None)
    if not callable(create) or not inspect.iscoroutinefunction(create):
        return False
    # A combined writer/reader/controller object would collapse the IAM
    # boundary even if it structurally satisfies SecretWriter.
    return not any(
        hasattr(candidate, operation)
        for operation in (
            "resolve_for_worker",
            "revoke",
            "delete_after_retention",
        )
    )


def _require_mutation_repository(
    repository: HostedAgentControlRepository,
    *,
    allow_non_durable_repository_for_tests: bool,
) -> None:
    if (
        getattr(repository, "durable", False) is not True
        and allow_non_durable_repository_for_tests is not True
    ):
        raise HostedControlPlaneError("non_durable_repository_forbidden")


class CapabilityCatalogService:
    """Safe capability catalogue and fail-closed readiness projection."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        hosted_agents_enabled: bool,
        credential_ingress_configured: bool,
    ) -> None:
        if not isinstance(registry, CapabilityRegistry):
            raise TypeError("registry must be CapabilityRegistry")
        if (
            type(hosted_agents_enabled) is not bool
            or type(credential_ingress_configured) is not bool
        ):
            raise TypeError("readiness flags must be booleans")
        _safe_persisted_identifier(
            registry.registry_version,
            error_code="invalid_identifier",
        )
        self._registry = registry
        self._hosted_agents_enabled = hosted_agents_enabled
        self._credential_ingress_configured = (
            credential_ingress_configured
        )

    def list_capabilities(self) -> tuple[CapabilityProjection, ...]:
        projections: list[CapabilityProjection] = []
        for capability in self._registry.list_public():
            try:
                _safe_persisted_identifier(
                    capability.provider_id,
                    error_code="invalid_identifier",
                )
                _safe_persisted_identifier(
                    capability.model_id,
                    error_code="invalid_identifier",
                )
                _safe_persisted_identifier(
                    capability.schema_version,
                    error_code="invalid_identifier",
                )
                validate_runtime_controlled_text(capability.display_name)
            except (ArenaIngressSecurityError, HostedControlPlaneError):
                # An unsafe registry entry is not advertised as selectable.
                continue
            projections.append(
                CapabilityProjection(
                    provider_id=capability.provider_id,
                    model_id=capability.model_id,
                    display_name=capability.display_name,
                    supports_structured_output=(
                        capability.supports_structured_output
                    ),
                    thinking_mode=cast(
                        Literal["unsupported", "optional", "always_on"],
                        capability.thinking_mode.value,
                    ),
                    thinking_can_toggle=capability.thinking_can_toggle,
                    effective_thinking_default=(
                        capability.effective_thinking_default
                    ),
                    max_output_tokens=min(
                        capability.max_output_tokens,
                        _MAX_HOSTED_OUTPUT_TOKENS,
                    ),
                    request_timeout_cap_ms=(
                        capability.request_timeout_cap_ms
                    ),
                    schema_version=capability.schema_version,
                )
            )
        return tuple(projections)

    def readiness(self) -> HostedReadinessProjection:
        capabilities = self.list_capabilities()
        reasons: list[
            Literal[
                "credential_ingress_unavailable",
                "hosted_agents_disabled",
                "no_enabled_models",
            ]
        ] = []
        if not self._hosted_agents_enabled:
            reasons.append("hosted_agents_disabled")
        if not self._credential_ingress_configured:
            reasons.append("credential_ingress_unavailable")
        if not capabilities:
            reasons.append("no_enabled_models")
        return HostedReadinessProjection(
            available=not reasons,
            hosted_agents_enabled=self._hosted_agents_enabled,
            credential_ingress_configured=self._credential_ingress_configured,
            enabled_model_count=len(capabilities),
            registry_version=self._registry.registry_version,
            reason_codes=tuple(reasons),
        )


class CredentialIngressService:
    """Write-only BYOK ingress with HMAC fingerprinting and no raw-key DB path."""

    def __init__(
        self,
        repository: HostedAgentControlRepository,
        *,
        secret_writer: SecretWriter,
        fingerprint_pepper: bytes,
        fingerprint_pepper_version: int,
        allow_non_durable_repository_for_tests: bool = False,
        id_factory: Callable[[str], str] = _new_id,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        _require_mutation_repository(
            repository,
            allow_non_durable_repository_for_tests=(
                allow_non_durable_repository_for_tests
            ),
        )
        if not _is_write_only_secret_writer(secret_writer):
            raise HostedControlPlaneError("secret_writer_not_write_only")
        if (
            type(fingerprint_pepper) is not bytes
            or len(fingerprint_pepper) < _MIN_PEPPER_BYTES
            or type(fingerprint_pepper_version) is not int
            or fingerprint_pepper_version <= 0
        ):
            raise HostedControlPlaneError("invalid_fingerprint_pepper")
        self._repository = repository
        self._secret_writer = secret_writer
        self._pepper = fingerprint_pepper
        self._pepper_version = fingerprint_pepper_version
        self._id_factory = id_factory
        self._clock = clock

    @property
    def ready(self) -> bool:
        return True

    async def create_credential(
        self,
        *,
        owner_user_id: str,
        request: CredentialIngressRequest,
    ) -> CredentialMetadata:
        owner = _safe_owner_id(owner_user_id)
        if not isinstance(request, CredentialIngressRequest):
            raise TypeError("request must be CredentialIngressRequest")
        provider_id = _safe_persisted_identifier(
            request.provider_id,
            error_code="invalid_identifier",
        )
        idempotency_key = _safe_persisted_identifier(
            request.idempotency_key,
            error_code="invalid_idempotency_key",
        )
        idempotency_key_digest = sha256_text_identifier(idempotency_key)

        # SecretStr keeps normal repr/dump paths redacted.  SecretWrite provides
        # the best-effort mutable buffer and is closed in every branch.
        try:
            secret_write = SecretWrite.from_text(
                request.api_key.get_secret_value()
            )
        except SecretStoreError:
            raise HostedControlPlaneError("invalid_secret_material") from None

        try:
            fingerprint = secret_write.hmac_sha256(self._pepper)

            now = self._clock()
            credential_id = _safe_persisted_identifier(
                self._id_factory("cred"),
                error_code="invalid_identifier",
            )
            secret_ref = SecretReference(
                f"arena402/hosted-model/{credential_id}"
            )
            credential = CredentialRecord(
                credential_id=credential_id,
                owner_user_id=owner,
                provider_id=provider_id,
                secret_ref=secret_ref.value,
                fingerprint=fingerprint,
                fingerprint_pepper_version=self._pepper_version,
                status=CredentialStatus.PENDING_WRITE,
                created_at=now,
                updated_at=now,
            )
            request_hash = sha256_identifier(
                {
                    "fingerprint": fingerprint,
                    "fingerprintPepperVersion": self._pepper_version,
                    "providerId": provider_id,
                }
            )
            try:
                reservation = await self._repository.reserve_credential(
                    credential=credential,
                    idempotency_key_digest=idempotency_key_digest,
                    request_hash=request_hash,
                )
            except ControlRepositoryError as exc:
                raise _map_repository_error(exc) from None
            except Exception:
                raise _repository_unavailable() from None

            if reservation.disposition is ReservationDisposition.REPLAY:
                if (
                    reservation.credential.status
                    is CredentialStatus.PENDING_WRITE
                ):
                    # The first attempt did not durably complete both sides of
                    # the DB/Secret-Manager handoff. A separate reconciler must
                    # determine whether the fixed secret_ref was written.
                    raise HostedControlPlaneError(
                        "credential_write_recovery_required"
                    )
                return _credential_projection(reservation.credential)

            try:
                written_ref = await self._secret_writer.create(
                    secret_ref,
                    secret_write,
                )
                if written_ref != secret_ref:
                    raise HostedControlPlaneError(
                        "secret_store_unavailable"
                    )
            except HostedControlPlaneError:
                raise
            except Exception:
                # The pending_write row remains recovery evidence.  Never fall
                # back to a business-table or process-environment plaintext.
                raise HostedControlPlaneError(
                    "secret_store_unavailable"
                ) from None

            try:
                mark_stored = (
                    self._repository
                    .mark_credential_stored_and_complete_idempotency
                )
                stored = await mark_stored(
                    owner_user_id=owner,
                    credential_id=credential_id,
                    idempotency_key_digest=idempotency_key_digest,
                    request_hash=request_hash,
                )
            except ControlRepositoryError as exc:
                raise _map_repository_error(exc) from None
            except Exception:
                raise _repository_unavailable() from None
            return _credential_projection(stored)
        finally:
            secret_write.close()

    async def get_credential(
        self,
        *,
        owner_user_id: str,
        credential_id: str,
    ) -> CredentialMetadata:
        owner = _safe_owner_id(owner_user_id)
        safe_credential_id = _safe_persisted_identifier(
            credential_id,
            error_code="invalid_identifier",
        )
        try:
            record = await self._repository.get_credential_for_owner(
                owner_user_id=owner,
                credential_id=safe_credential_id,
            )
        except Exception:
            raise _repository_unavailable() from None
        if record is None:
            raise HostedControlPlaneError("credential_not_found")
        return _credential_projection(record)

    async def list_credentials(
        self,
        *,
        owner_user_id: str,
    ) -> tuple[CredentialMetadata, ...]:
        owner = _safe_owner_id(owner_user_id)
        try:
            records = await self._repository.list_credentials_for_owner(
                owner_user_id=owner,
            )
        except Exception:
            raise _repository_unavailable() from None
        return tuple(_credential_projection(record) for record in records)


class HostedAgentService:
    """Owner-scoped create/list/detail service for Hosted Arena Agents."""

    def __init__(
        self,
        repository: HostedAgentControlRepository,
        *,
        capabilities: CapabilityRegistry,
        hosted_agents_enabled: bool,
        allow_non_durable_repository_for_tests: bool = False,
        id_factory: Callable[[str], str] = _new_id,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        _require_mutation_repository(
            repository,
            allow_non_durable_repository_for_tests=(
                allow_non_durable_repository_for_tests
            ),
        )
        if not isinstance(capabilities, CapabilityRegistry):
            raise TypeError("capabilities must be CapabilityRegistry")
        if type(hosted_agents_enabled) is not bool:
            raise TypeError("hosted_agents_enabled must be bool")
        _safe_persisted_identifier(
            capabilities.registry_version,
            error_code="invalid_identifier",
        )
        self._repository = repository
        self._capabilities = capabilities
        self._hosted_agents_enabled = hosted_agents_enabled
        self._id_factory = id_factory
        self._clock = clock

    async def create_hosted_agent(
        self,
        *,
        owner_user_id: str,
        request: HostedAgentCreateRequest,
    ) -> HostedAgentDetail:
        if not self._hosted_agents_enabled:
            raise HostedControlPlaneError("hosted_agents_disabled")
        owner = _safe_owner_id(owner_user_id)
        if not isinstance(request, HostedAgentCreateRequest):
            raise TypeError("request must be HostedAgentCreateRequest")
        strategy = _safe_strategy(request.strategy_instructions)
        display_name = _safe_display_name(request.display_name)
        credential_id = _safe_persisted_identifier(
            request.credential_id,
            error_code="invalid_identifier",
        )
        provider_id = _safe_persisted_identifier(
            request.provider_id,
            error_code="invalid_identifier",
        )
        model_id = _safe_persisted_identifier(
            request.model_id,
            error_code="invalid_identifier",
        )
        idempotency_key = _safe_persisted_identifier(
            request.idempotency_key,
            error_code="invalid_idempotency_key",
        )
        idempotency_key_digest = sha256_text_identifier(idempotency_key)
        request_hash = sha256_identifier(
            request.model_dump(
                mode="json",
                by_alias=True,
                exclude={"idempotency_key"},
            )
        )

        # Completed idempotency replay is authoritative even if mutable
        # prerequisites (credential lifecycle or the published capability
        # catalogue) have changed since the original transaction. The
        # repository repeats this check inside create_hosted_agent so a fresh
        # request racing a concurrent completion remains safe.
        try:
            replay = (
                await self._repository.get_hosted_agent_creation_replay(
                    owner_user_id=owner,
                    idempotency_key_digest=idempotency_key_digest,
                    request_hash=request_hash,
                )
            )
        except ControlRepositoryError as exc:
            raise _map_repository_error(exc) from None
        except Exception:
            raise _repository_unavailable() from None
        if replay is not None:
            return _agent_detail(replay)

        try:
            credential = await self._repository.get_credential_for_owner(
                owner_user_id=owner,
                credential_id=credential_id,
            )
        except Exception:
            raise _repository_unavailable() from None
        if credential is None:
            raise HostedControlPlaneError("credential_not_found")
        if credential.provider_id != provider_id:
            raise HostedControlPlaneError("provider_mismatch")
        public_capability = next(
            (
                capability
                for capability in self._capabilities.list_public()
                if (
                    capability.provider_id == provider_id
                    and capability.model_id == model_id
                )
            ),
            None,
        )
        if public_capability is None:
            raise HostedControlPlaneError("model_not_available")

        try:
            capability = self._capabilities.resolve(
                provider_id=provider_id,
                model_id=model_id,
                thinking_enabled=request.thinking_enabled,
                remaining_timeout_ms=_CAPABILITY_RESOLUTION_BUDGET_MS,
                requested_max_output_tokens=min(
                    _MAX_HOSTED_OUTPUT_TOKENS,
                    public_capability.max_output_tokens,
                ),
            )
        except CapabilityError as exc:
            raise _map_capability_error(exc) from None
        adapter_version = _safe_persisted_identifier(
            capability.adapter_version,
            error_code="invalid_identifier",
        )

        now = self._clock()
        agent_id = _safe_persisted_identifier(
            self._id_factory("agent"),
            error_code="invalid_identifier",
        )
        hosted_config_id = _safe_persisted_identifier(
            self._id_factory("hcfg"),
            error_code="invalid_identifier",
        )
        runtime_binding_id = _safe_persisted_identifier(
            self._id_factory("rbind"),
            error_code="invalid_identifier",
        )
        validation_job_id = _safe_persisted_identifier(
            self._id_factory("cval"),
            error_code="invalid_identifier",
        )
        # This exact snake_case snapshot is persisted by the PostgreSQL
        # repository and re-hashed during validation/recovery. API aliases are
        # deliberately not used for the internal immutable config contract.
        config_source = {
            "credential_id": credential.credential_id,
            "provider": capability.provider_id,
            "model": capability.model_id,
            "thinking_enabled": capability.thinking_enabled,
            "strategy_instructions": strategy,
            "prompt_version": HOSTED_AGENT_INSTRUCTION_VERSION_V1,
            "task_schema_version": AGENT_TASK_SCHEMA_VERSION_V1,
            "action_schema_version": AGENT_ACTION_SCHEMA_VERSION_V1,
            "capability_version": self._capabilities.registry_version,
            "adapter_version": adapter_version,
            "max_input_bytes": _MAX_INPUT_BYTES,
            "max_context_items": _MAX_CONTEXT_ITEMS,
            "max_output_tokens": capability.max_output_tokens,
        }
        record = HostedAgentRecord(
            agent_id=agent_id,
            owner_user_id=owner,
            display_name=display_name,
            identity_status=AgentIdentityStatus.ACTIVE,
            hosted_config_id=hosted_config_id,
            credential_id=credential.credential_id,
            provider_id=capability.provider_id,
            model_id=capability.model_id,
            thinking_enabled=capability.thinking_enabled,
            strategy_instructions=strategy,
            prompt_version=HOSTED_AGENT_INSTRUCTION_VERSION_V1,
            task_schema_version=AGENT_TASK_SCHEMA_VERSION_V1,
            action_schema_version=AGENT_ACTION_SCHEMA_VERSION_V1,
            capability_version=self._capabilities.registry_version,
            adapter_version=adapter_version,
            max_input_bytes=_MAX_INPUT_BYTES,
            max_context_items=_MAX_CONTEXT_ITEMS,
            max_output_tokens=capability.max_output_tokens,
            config_hash=sha256_identifier(config_source),
            provisioning_status=HostedProvisioningStatus.PROVISIONING,
            runtime_binding_id=runtime_binding_id,
            route_status=HostedProvisioningStatus.PROVISIONING,
            validation_job_id=validation_job_id,
            created_at=now,
            updated_at=now,
        )
        try:
            creation = await self._repository.create_hosted_agent(
                agent=record,
                idempotency_key_digest=idempotency_key_digest,
                request_hash=request_hash,
            )
        except ControlRepositoryError as exc:
            raise _map_repository_error(exc) from None
        except Exception:
            raise _repository_unavailable() from None
        return _agent_detail(creation.agent)

    async def get_hosted_agent(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
    ) -> HostedAgentDetail:
        owner = _safe_owner_id(owner_user_id)
        safe_agent_id = _safe_persisted_identifier(
            agent_id,
            error_code="invalid_identifier",
        )
        try:
            record = await self._repository.get_hosted_agent_for_owner(
                owner_user_id=owner,
                agent_id=safe_agent_id,
            )
        except Exception:
            raise _repository_unavailable() from None
        if record is None:
            # Cross-owner access and absence intentionally share one error.
            raise HostedControlPlaneError("agent_not_found")
        return _agent_detail(record)

    async def update_hosted_agent(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
        request: HostedAgentUpdateRequest,
    ) -> HostedAgentDetail:
        if not self._hosted_agents_enabled:
            raise HostedControlPlaneError("hosted_agents_disabled")
        owner = _safe_owner_id(owner_user_id)
        safe_agent_id = _safe_persisted_identifier(
            agent_id,
            error_code="invalid_identifier",
        )
        if not isinstance(request, HostedAgentUpdateRequest):
            raise TypeError("request must be HostedAgentUpdateRequest")
        provider_id = _safe_persisted_identifier(
            request.provider_id,
            error_code="invalid_identifier",
        )
        model_id = _safe_persisted_identifier(
            request.model_id,
            error_code="invalid_identifier",
        )
        strategy = _safe_strategy(request.strategy_instructions)
        idempotency_key = _safe_persisted_identifier(
            request.idempotency_key,
            error_code="invalid_idempotency_key",
        )
        idempotency_key_digest = sha256_text_identifier(idempotency_key)
        request_hash = sha256_identifier(
            request.model_dump(
                mode="json",
                by_alias=True,
                exclude={"idempotency_key"},
            )
        )
        try:
            replay = await self._repository.get_hosted_agent_update_replay(
                owner_user_id=owner,
                idempotency_key_digest=idempotency_key_digest,
                request_hash=request_hash,
            )
        except ControlRepositoryError as exc:
            raise _map_repository_error(exc) from None
        except Exception:
            raise _repository_unavailable() from None
        if replay is not None:
            return _agent_detail(replay)
        try:
            current = await self._repository.get_hosted_agent_for_owner(
                owner_user_id=owner,
                agent_id=safe_agent_id,
            )
        except Exception:
            raise _repository_unavailable() from None
        if current is None:
            raise HostedControlPlaneError("agent_not_found")
        if current.provider_id != provider_id:
            raise HostedControlPlaneError("provider_mismatch")
        try:
            credential = await self._repository.get_credential_for_owner(
                owner_user_id=owner,
                credential_id=current.credential_id,
            )
        except Exception:
            raise _repository_unavailable() from None
        if credential is None:
            raise HostedControlPlaneError("credential_not_found")
        if credential.status is not CredentialStatus.VALID:
            raise HostedControlPlaneError("credential_not_usable")

        public_capability = next(
            (
                capability
                for capability in self._capabilities.list_public()
                if (
                    capability.provider_id == provider_id
                    and capability.model_id == model_id
                )
            ),
            None,
        )
        if public_capability is None:
            raise HostedControlPlaneError("model_not_available")
        try:
            capability = self._capabilities.resolve(
                provider_id=provider_id,
                model_id=model_id,
                thinking_enabled=request.thinking_enabled,
                remaining_timeout_ms=_CAPABILITY_RESOLUTION_BUDGET_MS,
                requested_max_output_tokens=min(
                    _MAX_HOSTED_OUTPUT_TOKENS,
                    public_capability.max_output_tokens,
                ),
            )
        except CapabilityError as exc:
            raise _map_capability_error(exc) from None
        now = self._clock()
        config_source = {
            "credential_id": current.credential_id,
            "provider": capability.provider_id,
            "model": capability.model_id,
            "thinking_enabled": capability.thinking_enabled,
            "strategy_instructions": strategy,
            "prompt_version": HOSTED_AGENT_INSTRUCTION_VERSION_V1,
            "task_schema_version": AGENT_TASK_SCHEMA_VERSION_V1,
            "action_schema_version": AGENT_ACTION_SCHEMA_VERSION_V1,
            "capability_version": self._capabilities.registry_version,
            "adapter_version": capability.adapter_version,
            "max_input_bytes": _MAX_INPUT_BYTES,
            "max_context_items": _MAX_CONTEXT_ITEMS,
            "max_output_tokens": capability.max_output_tokens,
        }
        candidate = current.model_copy(
            update={
                "provider_id": capability.provider_id,
                "model_id": capability.model_id,
                "thinking_enabled": capability.thinking_enabled,
                "strategy_instructions": strategy,
                "prompt_version": HOSTED_AGENT_INSTRUCTION_VERSION_V1,
                "task_schema_version": AGENT_TASK_SCHEMA_VERSION_V1,
                "action_schema_version": AGENT_ACTION_SCHEMA_VERSION_V1,
                "capability_version": self._capabilities.registry_version,
                "adapter_version": capability.adapter_version,
                "max_input_bytes": _MAX_INPUT_BYTES,
                "max_context_items": _MAX_CONTEXT_ITEMS,
                "max_output_tokens": capability.max_output_tokens,
                "config_hash": sha256_identifier(config_source),
                "provisioning_status": HostedProvisioningStatus.PROVISIONING,
                "route_status": HostedProvisioningStatus.PROVISIONING,
                "validation_job_id": _safe_persisted_identifier(
                    self._id_factory("cval"),
                    error_code="invalid_identifier",
                ),
                "updated_at": now,
            }
        )
        try:
            updated = await self._repository.update_hosted_agent(
                agent=candidate,
                expected_config_hash=current.config_hash,
                idempotency_key_digest=idempotency_key_digest,
                request_hash=request_hash,
            )
        except ControlRepositoryError as exc:
            raise _map_repository_error(exc) from None
        except Exception:
            raise _repository_unavailable() from None
        return _agent_detail(updated)

    async def list_hosted_agents(
        self,
        *,
        owner_user_id: str,
    ) -> tuple[HostedAgentSummary, ...]:
        owner = _safe_owner_id(owner_user_id)
        try:
            records = await self._repository.list_hosted_agents_for_owner(
                owner_user_id=owner,
            )
        except Exception:
            raise _repository_unavailable() from None
        return tuple(_agent_summary(record) for record in records)


__all__ = [
    "CapabilityCatalogService",
    "ControlPlaneErrorCode",
    "CredentialIngressService",
    "HostedAgentService",
    "HostedControlPlaneError",
]
