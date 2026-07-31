"""Arena-owned task orchestration and result application primitives."""

from .connector_registration import (
    ConnectorAgentRegistration,
    ConnectorRegistrationError,
    PostgresConnectorArenaRegistrar,
)
from .finalizer import ArenaDeadlineFinalizer
from .ingress_security import (
    ArenaIngressSecurityError,
    secure_config_snapshot,
    validate_runtime_controlled_text,
    validate_runtime_result_identifiers,
)
from .postgres_repository import PostgresArenaCoreRepository
from .models import ConnectorTaskClaim, ConnectorTaskRoute
from .public_output_policy import (
    PUBLIC_OUTPUT_POLICY_VERSION,
    PublicOutputDecision,
    PublicOutputPolicy,
)
from .repository import (
    ArenaCoreRepository,
    ArenaIdempotencyConflictError,
    ArenaRepositoryError,
    ArenaResultConflictError,
    ArenaTaskNotFoundError,
    MemoryArenaCoreRepository,
)
from .result_consumer import ArenaResultConsumer
from .result_sink import ArenaResultSink
from .task_factory import ArenaTaskFactory
from .participation import (
    ArenaParticipationError,
    GameParticipation,
    LocalAgentRegistration,
    PostgresArenaParticipationRepository,
)

__all__ = [
    "ArenaCoreRepository",
    "ConnectorAgentRegistration",
    "ConnectorRegistrationError",
    "ConnectorTaskClaim",
    "ConnectorTaskRoute",
    "ArenaDeadlineFinalizer",
    "ArenaIdempotencyConflictError",
    "ArenaIngressSecurityError",
    "ArenaRepositoryError",
    "ArenaParticipationError",
    "ArenaResultConflictError",
    "ArenaResultConsumer",
    "ArenaResultSink",
    "ArenaTaskFactory",
    "ArenaTaskNotFoundError",
    "MemoryArenaCoreRepository",
    "GameParticipation",
    "LocalAgentRegistration",
    "PostgresArenaParticipationRepository",
    "PostgresArenaCoreRepository",
    "PostgresConnectorArenaRegistrar",
    "PUBLIC_OUTPUT_POLICY_VERSION",
    "PublicOutputDecision",
    "PublicOutputPolicy",
    "secure_config_snapshot",
    "validate_runtime_controlled_text",
    "validate_runtime_result_identifiers",
]
