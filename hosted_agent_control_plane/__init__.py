"""Hosted Agent control plane, persistence, and lifecycle boundaries."""

from .credential_controller import (
    CredentialLifecycleJob,
    CredentialLifecycleRepository,
    DurableCredentialController,
    PostgresCredentialLifecycleRepository,
)
from .models import (
    CONTROL_PLANE_SCHEMA_VERSION_V1,
    AgentIdentityStatus,
    CapabilityProjection,
    CredentialIngressRequest,
    CredentialMetadata,
    CredentialRecord,
    CredentialReservation,
    CredentialStatus,
    HostedAgentCreateRequest,
    HostedAgentCreation,
    HostedAgentDetail,
    HostedAgentRecord,
    HostedAgentSummary,
    HostedProvisioningStatus,
    HostedReadinessProjection,
    ReservationDisposition,
)
from .repository import (
    ControlRepositoryError,
    HostedAgentControlRepository,
    MemoryHostedAgentControlRepository,
)
from .postgres_repository import PostgresHostedAgentControlRepository
from .production import (
    ProductionHostedControlBundle,
    build_production_hosted_control,
)
from .local_development import (
    LocalHostedControlBundle,
    build_local_hosted_control,
)
from .services import (
    CapabilityCatalogService,
    CredentialIngressService,
    HostedAgentService,
    HostedControlPlaneError,
)

__all__ = [
    "CredentialLifecycleJob",
    "CredentialLifecycleRepository",
    "DurableCredentialController",
    "PostgresCredentialLifecycleRepository",
    "CONTROL_PLANE_SCHEMA_VERSION_V1",
    "AgentIdentityStatus",
    "CapabilityCatalogService",
    "CapabilityProjection",
    "ControlRepositoryError",
    "CredentialIngressRequest",
    "CredentialIngressService",
    "CredentialMetadata",
    "CredentialRecord",
    "CredentialReservation",
    "CredentialStatus",
    "HostedAgentControlRepository",
    "HostedAgentCreateRequest",
    "HostedAgentCreation",
    "HostedAgentDetail",
    "HostedAgentRecord",
    "HostedAgentService",
    "HostedAgentSummary",
    "HostedControlPlaneError",
    "HostedProvisioningStatus",
    "HostedReadinessProjection",
    "MemoryHostedAgentControlRepository",
    "PostgresHostedAgentControlRepository",
    "ProductionHostedControlBundle",
    "LocalHostedControlBundle",
    "ReservationDisposition",
    "build_production_hosted_control",
    "build_local_hosted_control",
]
