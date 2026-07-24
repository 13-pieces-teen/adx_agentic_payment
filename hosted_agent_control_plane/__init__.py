"""Phase 4 network-independent Hosted Agent control-plane foundation.

This package is not a production API or a production persistence adapter.  It
defines strict domain/service contracts and an explicitly test-only in-memory
repository so the later FastAPI and PostgreSQL adapters have a security
boundary to implement.
"""

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
