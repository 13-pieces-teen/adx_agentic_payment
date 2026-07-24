"""Strict, network-independent models for the Hosted Agent control plane.

The public projections in this module intentionally exclude Secret Manager
references, full credential fingerprints, idempotency request hashes, and
private persistence metadata.  The raw provider key exists only on
``CredentialIngressRequest`` as a redacted ``SecretStr`` and is never part of
any repository model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
)


CONTROL_PLANE_SCHEMA_VERSION_V1: Final[
    Literal["arena.hosted-control-plane.v1"]
] = (
    "arena.hosted-control-plane.v1"
)


def _to_camel(field_name: str) -> str:
    head, *tail = field_name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class StrictControlModel(BaseModel):
    """Immutable and non-coercing model used at every service boundary."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        strict=True,
    )


Identifier: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
ProviderId: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    ),
]
ModelId: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
IdempotencyKey: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
HashIdentifier: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
]
HexDigest: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]
UtcDateTime: TypeAlias = Annotated[AwareDatetime, Field()]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CredentialStatus(str, Enum):
    PENDING_WRITE = "pending_write"
    STORED = "stored"
    PENDING_VALIDATION = "pending_validation"
    VALID = "valid"
    INVALID = "invalid"
    REVOKING = "revoking"
    REVOKED = "revoked"


class HostedProvisioningStatus(str, Enum):
    PROVISIONING = "provisioning"
    READY = "ready"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class AgentIdentityStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class CredentialIngressRequest(StrictControlModel):
    """The only control-plane model allowed to contain raw provider material."""

    provider_id: ProviderId
    api_key: SecretStr = Field(repr=False)
    idempotency_key: IdempotencyKey

    @field_validator("api_key")
    @classmethod
    def reject_empty_secret(cls, value: SecretStr) -> SecretStr:
        # Do not include the rejected value in the error text.
        if not value.get_secret_value():
            raise ValueError("provider key must not be empty")
        return value


class HostedAgentCreateRequest(StrictControlModel):
    display_name: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=100,
            strip_whitespace=True,
        ),
    ]
    credential_id: Identifier
    provider_id: ProviderId
    model_id: ModelId
    thinking_enabled: bool
    strategy_instructions: Annotated[
        str,
        StringConstraints(max_length=4000),
    ] = Field(default="", repr=False)
    idempotency_key: IdempotencyKey


class CredentialRecord(StrictControlModel):
    """Internal persistence model; it can reference but never contain a key."""

    credential_id: Identifier
    owner_user_id: Identifier
    provider_id: ProviderId
    secret_ref: Annotated[
        str,
        StringConstraints(
            min_length=3,
            max_length=512,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
        ),
    ] = Field(repr=False)
    fingerprint: HexDigest = Field(repr=False)
    fingerprint_pepper_version: Annotated[int, Field(gt=0)]
    status: CredentialStatus
    created_at: UtcDateTime
    updated_at: UtcDateTime


class CredentialMetadata(StrictControlModel):
    """Owner-scoped safe projection returned by list/detail/create."""

    credential_id: Identifier
    provider_id: ProviderId
    status: CredentialStatus
    fingerprint_hint: Annotated[
        str,
        StringConstraints(pattern=r"^hmac-sha256:[0-9a-f]{12}$"),
    ]
    created_at: UtcDateTime
    updated_at: UtcDateTime
    schema_version: Literal["arena.hosted-control-plane.v1"] = (
        CONTROL_PLANE_SCHEMA_VERSION_V1
    )


class HostedAgentRecord(StrictControlModel):
    """Internal atomic Agent + Config + Runtime Binding projection."""

    agent_id: Identifier
    owner_user_id: Identifier
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    identity_status: AgentIdentityStatus
    hosted_config_id: Identifier
    credential_id: Identifier
    provider_id: ProviderId
    model_id: ModelId
    thinking_enabled: bool
    strategy_instructions: Annotated[
        str,
        StringConstraints(max_length=4000),
    ] = Field(repr=False)
    prompt_version: Identifier
    task_schema_version: Identifier
    action_schema_version: Identifier
    capability_version: Identifier
    adapter_version: Identifier
    max_input_bytes: Annotated[int, Field(gt=0, le=1_048_576)]
    max_context_items: Annotated[int, Field(gt=0, le=10_000)]
    max_output_tokens: Annotated[int, Field(gt=0, le=65_536)]
    config_hash: HashIdentifier
    provisioning_status: HostedProvisioningStatus
    runtime_binding_id: Identifier
    route_status: HostedProvisioningStatus
    validation_job_id: Identifier
    created_at: UtcDateTime
    updated_at: UtcDateTime


class HostedAgentSummary(StrictControlModel):
    agent_id: Identifier
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    provider_id: ProviderId
    model_id: ModelId
    thinking_enabled: bool
    provisioning_status: HostedProvisioningStatus
    route_status: HostedProvisioningStatus
    created_at: UtcDateTime
    updated_at: UtcDateTime
    schema_version: Literal["arena.hosted-control-plane.v1"] = (
        CONTROL_PLANE_SCHEMA_VERSION_V1
    )


class HostedAgentDetail(HostedAgentSummary):
    """Owner-only detail without Secret Manager or idempotency internals."""

    credential_id: Identifier
    strategy_instructions: Annotated[
        str,
        StringConstraints(max_length=4000),
    ] = Field(default="", repr=False)


class CapabilityProjection(StrictControlModel):
    provider_id: ProviderId
    model_id: ModelId
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    supports_structured_output: bool
    thinking_mode: Literal["unsupported", "optional", "always_on"]
    thinking_can_toggle: bool
    effective_thinking_default: bool
    max_output_tokens: Annotated[int, Field(gt=0)]
    request_timeout_cap_ms: Annotated[int, Field(gt=0)]
    schema_version: Identifier


ReadinessReason: TypeAlias = Literal[
    "credential_ingress_unavailable",
    "hosted_agents_disabled",
    "no_enabled_models",
]


class HostedReadinessProjection(StrictControlModel):
    available: bool
    hosted_agents_enabled: bool
    credential_ingress_configured: bool
    enabled_model_count: Annotated[int, Field(ge=0)]
    registry_version: Identifier
    reason_codes: tuple[ReadinessReason, ...] = ()
    schema_version: Literal["arena.hosted-control-plane.v1"] = (
        CONTROL_PLANE_SCHEMA_VERSION_V1
    )


class ReservationDisposition(str, Enum):
    CREATED = "created"
    REPLAY = "replay"


class CredentialReservation(StrictControlModel):
    disposition: ReservationDisposition
    credential: CredentialRecord


class HostedAgentCreation(StrictControlModel):
    disposition: ReservationDisposition
    agent: HostedAgentRecord


__all__ = [
    "CONTROL_PLANE_SCHEMA_VERSION_V1",
    "AgentIdentityStatus",
    "CapabilityProjection",
    "CredentialIngressRequest",
    "CredentialMetadata",
    "CredentialRecord",
    "CredentialReservation",
    "CredentialStatus",
    "HostedAgentCreateRequest",
    "HostedAgentCreation",
    "HostedAgentDetail",
    "HostedAgentRecord",
    "HostedAgentSummary",
    "HostedProvisioningStatus",
    "HostedReadinessProjection",
    "ReservationDisposition",
    "StrictControlModel",
    "utc_now",
]
