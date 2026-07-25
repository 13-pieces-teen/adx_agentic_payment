"""Production composition for the Hosted Agent HTTP control plane."""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass

from connector_gateway.auth import ConnectorAuth
from hosted_agent_runtime.production_providers import (
    build_production_capability_registry,
)
from hosted_agent_runtime.production_secrets import (
    build_production_secret_writer,
    close_secret_port,
    initialize_secret_port,
)
from hosted_agent_runtime.secret_store import SecretWriter

from .postgres_repository import PostgresHostedAgentControlRepository
from .services import (
    CapabilityCatalogService,
    CredentialIngressService,
    HostedAgentService,
)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for Hosted Agents")
    return value


def _true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


@dataclass(slots=True)
class ProductionHostedControlBundle:
    repository: PostgresHostedAgentControlRepository
    catalog: CapabilityCatalogService
    credential_service: CredentialIngressService
    agent_service: HostedAgentService
    auth: ConnectorAuth
    secret_writer: SecretWriter

    async def initialize(self) -> None:
        await self.repository.initialize()
        try:
            await initialize_secret_port(self.secret_writer)
        except BaseException:
            await self.repository.close()
            raise

    async def close(self) -> None:
        await close_secret_port(self.secret_writer)
        await self.repository.close()


def build_production_hosted_control(
    auth: ConnectorAuth,
) -> ProductionHostedControlBundle:
    if not _true("ADX_HOSTED_AGENTS_ENABLED"):
        raise RuntimeError("Hosted Agents are not enabled")
    try:
        pepper = base64.b64decode(
            _required("ADX_HOSTED_FINGERPRINT_PEPPER_B64"),
            validate=True,
        )
    except (ValueError, binascii.Error):
        raise RuntimeError(
            "ADX_HOSTED_FINGERPRINT_PEPPER_B64 must be valid base64"
        ) from None
    if len(pepper) < 32:
        raise RuntimeError(
            "ADX_HOSTED_FINGERPRINT_PEPPER_B64 must decode to 32+ bytes"
        )

    database_url = _required("ADX_HOSTED_CONTROL_DATABASE_URL")
    repository = PostgresHostedAgentControlRepository(database_url)
    secret_writer = build_production_secret_writer(database_url)
    registry = build_production_capability_registry()
    catalog = CapabilityCatalogService(
        registry,
        hosted_agents_enabled=True,
        credential_ingress_configured=True,
    )
    credential_service = CredentialIngressService(
        repository,
        secret_writer=secret_writer,
        fingerprint_pepper=pepper,
        fingerprint_pepper_version=int(
            os.getenv("ADX_HOSTED_FINGERPRINT_PEPPER_VERSION", "1")
        ),
    )
    agent_service = HostedAgentService(
        repository,
        capabilities=registry,
        hosted_agents_enabled=True,
    )
    return ProductionHostedControlBundle(
        repository=repository,
        catalog=catalog,
        credential_service=credential_service,
        agent_service=agent_service,
        auth=auth,
        secret_writer=secret_writer,
    )


__all__ = [
    "ProductionHostedControlBundle",
    "build_production_hosted_control",
]
