"""Platform-side control plane for outbound local Agent Connectors."""

from .api import create_connector_router, create_production_connector_router
from .config import ConnectorConfigurationError, ConnectorGatewayConfig
from .persistent_service import PersistentConnectorGateway
from .arena_adapter import ConnectorArenaRoute, ConnectorArenaRuntimeAdapter
from .arena_dispatcher import ConnectorArenaTaskDispatcher
from .production import ProductionConnectorBundle, build_production_connector
from .service import ConnectorGateway

__all__ = [
    "ConnectorConfigurationError",
    "ConnectorGateway",
    "ConnectorGatewayConfig",
    "PersistentConnectorGateway",
    "ConnectorArenaRoute",
    "ConnectorArenaTaskDispatcher",
    "ConnectorArenaRuntimeAdapter",
    "ProductionConnectorBundle",
    "build_production_connector",
    "create_connector_router",
    "create_production_connector_router",
]
