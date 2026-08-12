"""Composition root used by the production ASGI application."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter

from .api import ArenaConnectorRegistrar, create_production_connector_router
from .auth import ConnectorAuth
from .config import ConnectorGatewayConfig
from .github_oauth import GithubOAuthClient, HttpxGithubOAuthClient
from .persistent_service import PersistentConnectorGateway
from .postgres_repository import PostgresConnectorRepository
from .repository import ConnectorRepository


@dataclass
class ProductionConnectorBundle:
    config: ConnectorGatewayConfig
    repository: ConnectorRepository
    service: PersistentConnectorGateway
    auth: ConnectorAuth
    router: APIRouter

    async def initialize(self) -> None:
        """Connect to PostgreSQL and seed the one-use bootstrap invitation."""
        await self.auth.initialize()
        await self.service.initialize()

    async def close(self) -> None:
        await self.service.close()


def build_production_connector(
    config: ConnectorGatewayConfig | None = None,
    repository: ConnectorRepository | None = None,
    github_oauth_client: GithubOAuthClient | None = None,
    arena_registrar: ArenaConnectorRegistrar | None = None,
    *,
    include_websocket: bool = True,
) -> ProductionConnectorBundle:
    """Build a fail-closed production bundle without performing network I/O."""

    resolved_config = config or ConnectorGatewayConfig.from_env()
    resolved_repository = repository or PostgresConnectorRepository(
        resolved_config.database_url
    )
    service = PersistentConnectorGateway(
        resolved_repository,
        verification_uri=f"{resolved_config.public_app_url}/connect",
        max_pending_pairings=resolved_config.max_pending_pairings,
    )
    resolved_github_oauth_client = github_oauth_client
    if (
        resolved_github_oauth_client is None
        and resolved_config.github_oauth_client_id
        and resolved_config.github_oauth_client_secret
    ):
        resolved_github_oauth_client = HttpxGithubOAuthClient(
            resolved_config.github_oauth_client_id,
            resolved_config.github_oauth_client_secret,
            relay_url=resolved_config.github_oauth_relay_url,
        )
    auth = ConnectorAuth(
        resolved_repository,
        resolved_config,
        github_oauth_client=resolved_github_oauth_client,
    )
    return ProductionConnectorBundle(
        config=resolved_config,
        repository=resolved_repository,
        service=service,
        auth=auth,
        router=create_production_connector_router(
            service,
            auth,
            arena_registrar=arena_registrar,
            include_websocket=include_websocket,
        ),
    )
