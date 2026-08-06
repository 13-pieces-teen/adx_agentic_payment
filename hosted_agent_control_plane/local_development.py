"""Explicit local-only Hosted Agent composition.

This composition keeps API and Worker in one process so their ephemeral Secret
Store can be shared without writing Provider keys to PostgreSQL or local files.
It is intentionally rejected outside ``ADX_ENV=development``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
from dataclasses import dataclass, field

from connector_gateway.auth import ConnectorAuth
from hosted_agent_runtime.postgres_worker import (
    DurableHostedWorker,
    PostgresHostedWorkerRepository,
)
from hosted_agent_runtime.model_factory import PydanticModelFactory
from hosted_agent_runtime.production_providers import (
    ProductionProviderBundle,
    build_production_provider_bundle,
)
from hosted_agent_runtime.secret_store import MemorySecretStore

from .postgres_repository import PostgresHostedAgentControlRepository
from .services import (
    CapabilityCatalogService,
    CredentialIngressService,
    HostedAgentService,
)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for local Hosted Agents")
    return value


def _true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


@dataclass(slots=True)
class LocalHostedControlBundle:
    repository: PostgresHostedAgentControlRepository
    worker_repository: PostgresHostedWorkerRepository
    providers: ProductionProviderBundle
    worker: DurableHostedWorker
    catalog: CapabilityCatalogService
    credential_service: CredentialIngressService
    agent_service: HostedAgentService
    auth: ConnectorAuth
    _worker_task: asyncio.Task[None] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    async def initialize(self) -> None:
        await self.repository.initialize()
        await self.worker_repository.initialize()
        self._worker_task = asyncio.create_task(
            self.worker.run_forever(
                poll_seconds=float(
                    os.getenv("ADX_HOSTED_WORKER_POLL_SECONDS", "0.25")
                )
            ),
            name="arena402-local-hosted-worker",
        )

    async def close(self) -> None:
        self.worker.stop()
        if self._worker_task is not None:
            await self._worker_task
            self._worker_task = None
        await self.providers.close()
        await self.worker_repository.close()
        await self.repository.close()


def build_local_hosted_control(
    auth: ConnectorAuth,
) -> LocalHostedControlBundle:
    if os.getenv("ADX_ENV", "").strip().lower() != "development":
        raise RuntimeError(
            "ADX_HOSTED_LOCAL_DEV is allowed only with ADX_ENV=development"
        )
    if not _true("ADX_HOSTED_LOCAL_DEV"):
        raise RuntimeError("ADX_HOSTED_LOCAL_DEV must be explicitly enabled")
    if _true("ADX_CONNECTOR_UNSAFE_DEMO"):
        raise RuntimeError(
            "Local Hosted Agents require authenticated Connector mode"
        )

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

    control_repository = PostgresHostedAgentControlRepository(
        _required("ADX_HOSTED_CONTROL_DATABASE_URL")
    )
    worker_repository = PostgresHostedWorkerRepository(
        _required("ADX_HOSTED_WORKER_DATABASE_URL")
    )
    secret_ports = MemorySecretStore.for_local_development().ports
    providers = build_production_provider_bundle()
    registry = providers.registry
    catalog = CapabilityCatalogService(
        registry,
        hosted_agents_enabled=True,
        credential_ingress_configured=True,
    )
    credential_service = CredentialIngressService(
        control_repository,
        secret_writer=secret_ports.writer,
        fingerprint_pepper=pepper,
        fingerprint_pepper_version=1,
    )
    agent_service = HostedAgentService(
        control_repository,
        capabilities=registry,
        hosted_agents_enabled=True,
    )
    worker = DurableHostedWorker(
        repository=worker_repository,
        providers=providers,
        secret_reader=secret_ports.reader,
        worker_id="hosted-worker-local-dev",
        lease_seconds=60,
        task_concurrency=int(
            os.getenv("ADX_HOSTED_WORKER_TASK_CONCURRENCY", "12")
        ),
        model_factory=PydanticModelFactory(providers.registry),
    )
    return LocalHostedControlBundle(
        repository=control_repository,
        worker_repository=worker_repository,
        providers=providers,
        worker=worker,
        catalog=catalog,
        credential_service=credential_service,
        agent_service=agent_service,
        auth=auth,
    )


__all__ = [
    "LocalHostedControlBundle",
    "build_local_hosted_control",
]
