"""Arena 402 HTTP composition root.

This module mounts the current Connector, Hosted Agent, Arena participation,
and Pawnhouse game surfaces. Arena business state remains authoritative in the
Arena repositories; Runtime and Connector acknowledgements never mutate it
directly.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from arena_core import (
    PostgresArenaCoreRepository,
    PostgresArenaParticipationRepository,
    PostgresConnectorArenaRegistrar,
)
from arena_game import (
    EvmJsonRpcConfirmationReader,
    PawnhouseGameOrchestrator,
    PawnhouseHostedCoordinator,
    PostgresPawnhouseRepository,
)
from connector_gateway import (
    ConnectorGateway,
    ProductionConnectorBundle,
    build_production_connector,
    create_connector_router,
)
from hosted_agent_control_plane import (
    CapabilityCatalogService,
    LocalHostedControlBundle,
    ProductionHostedControlBundle,
    build_local_hosted_control,
    build_production_hosted_control,
)
from hosted_agent_runtime import CapabilityRegistry
from web.arena_participation_api import create_arena_participation_router
from web.game_operator_api import create_game_operator_router
from web.hosted_agent_api import create_hosted_agent_router
from web.pawnhouse_api import (
    create_pawnhouse_read_router,
    create_pawnhouse_router,
)


def _is_loopback_client(scope: Scope) -> bool:
    client = scope.get("client")
    if not client:
        return False
    host = str(client[0]).split("%", 1)[0]
    # Starlette's in-process TestClient uses this sentinel; an ASGI server
    # supplies the real peer address and cannot be overridden by HTTP headers.
    if host == "testclient":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _ConnectorLoopbackOnlyMiddleware:
    """Fail closed when the unauthenticated demo plane is reached remotely."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        is_connector_request = scope["type"] in {"http", "websocket"} and scope.get(
            "path", ""
        ).startswith("/api/connectors")
        if not is_connector_request or _is_loopback_client(scope):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            response = JSONResponse(
                {"detail": "Unsafe Connector demo is restricted to loopback clients"},
                status_code=403,
            )
            await response(scope, receive, send)
            return

        await send(
            {
                "type": "websocket.close",
                # Before websocket.accept this rejects the upgrade. Uvicorn
                # exposes it to the caller as an HTTP 403 handshake.
                "code": 1008,
                "reason": "Unsafe Connector demo is restricted to loopback clients",
            }
        )


def _mount_connector_gateway(
    app: FastAPI, connector_demo_enabled: Optional[bool]
) -> None:
    """Mount the unauthenticated MVP control plane only after an explicit opt-in."""

    if connector_demo_enabled is None:
        connector_demo_enabled = os.getenv(
            "ADX_CONNECTOR_UNSAFE_DEMO", ""
        ).strip().lower() in {"1", "true", "yes"}
    app.state.connector_gateway_enabled = connector_demo_enabled
    if not connector_demo_enabled:
        return
    app.add_middleware(_ConnectorLoopbackOnlyMiddleware)
    connector_gateway = ConnectorGateway()
    app.state.connector_gateway = connector_gateway
    app.include_router(create_connector_router(connector_gateway))


def _production_connector_enabled(
    connector_demo_enabled: Optional[bool],
) -> bool:
    if connector_demo_enabled is not None:
        return False
    connector_mode = os.getenv("ADX_CONNECTOR_MODE", "").strip().lower()
    environment = os.getenv("ADX_ENV", "").strip().lower()
    return connector_mode == "production" or environment == "production"


def _hosted_agents_requested() -> bool:
    return os.getenv(
        "ADX_HOSTED_AGENTS_ENABLED",
        "",
    ).strip().lower() in {"1", "true", "yes"}


def _hosted_local_dev_requested() -> bool:
    return os.getenv(
        "ADX_HOSTED_LOCAL_DEV",
        "",
    ).strip().lower() in {"1", "true", "yes"}


def _arena_participation_requested() -> bool:
    return os.getenv(
        "ADX_ARENA_PARTICIPATION_ENABLED",
        "",
    ).strip().lower() in {"1", "true", "yes"}


def _pawnhouse_dev_requested() -> bool:
    return os.getenv(
        "ADX_ARENA_DEV_CONTROL",
        "",
    ).strip().lower() in {"1", "true", "yes"}


def _pawnhouse_core_requested() -> bool:
    return os.getenv(
        "ADX_ARENA_CORE_ENABLED",
        "",
    ).strip().lower() in {"1", "true", "yes"}


def _allowed_origins(production: bool) -> list[str]:
    configured = [
        value.strip().rstrip("/")
        for value in os.getenv("ADX_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    ]
    if configured:
        if production and "*" in configured:
            raise RuntimeError("ADX_ALLOWED_ORIGINS must not contain '*' in production")
        return configured

    public_app_url = os.getenv("ADX_PUBLIC_APP_URL", "").strip()
    if public_app_url:
        parsed = urlsplit(public_app_url)
        if parsed.scheme and parsed.netloc:
            return [f"{parsed.scheme}://{parsed.netloc}"]
    if production:
        raise RuntimeError(
            "ADX_ALLOWED_ORIGINS or ADX_PUBLIC_APP_URL is required in production"
        )
    return ["http://localhost:3000", "http://127.0.0.1:3000"]


def create_app(connector_demo_enabled: Optional[bool] = None) -> FastAPI:
    production_connector = _production_connector_enabled(connector_demo_enabled)
    environment = os.getenv("ADX_ENV", "").strip().lower()
    pawnhouse_dev_enabled = _pawnhouse_dev_requested()
    pawnhouse_core_enabled = _pawnhouse_core_requested() or pawnhouse_dev_enabled
    if pawnhouse_dev_enabled and environment != "development":
        raise RuntimeError(
            "ADX_ARENA_DEV_CONTROL is allowed only with ADX_ENV=development"
        )
    if _hosted_local_dev_requested() and not _hosted_agents_requested():
        raise RuntimeError(
            "ADX_HOSTED_LOCAL_DEV requires ADX_HOSTED_AGENTS_ENABLED=true"
        )

    connector_bundle: ProductionConnectorBundle | None = None
    connector_arena_registrar: PostgresConnectorArenaRegistrar | None = None
    if production_connector:
        if _arena_participation_requested():
            registrar_dsn = (
                os.getenv("ADX_ARENA_API_DATABASE_URL")
                or os.getenv("ADX_HOSTED_CONTROL_DATABASE_URL")
                or os.getenv("ADX_CONNECTOR_DATABASE_URL")
                or ""
            ).strip()
            connector_arena_registrar = PostgresConnectorArenaRegistrar(
                registrar_dsn
            )
        # Validate every security-sensitive production setting before the
        # process starts accepting traffic.
        connector_bundle = build_production_connector(
            arena_registrar=connector_arena_registrar
        )

    hosted_bundle: (
        ProductionHostedControlBundle | LocalHostedControlBundle | None
    ) = None
    if _hosted_agents_requested():
        if connector_bundle is None:
            raise RuntimeError(
                "Hosted Agent creation requires the authenticated production "
                "Connector control plane"
            )
        if _hosted_local_dev_requested():
            hosted_bundle = build_local_hosted_control(connector_bundle.auth)
        else:
            hosted_bundle = build_production_hosted_control(connector_bundle.auth)

    arena_participation: PostgresArenaParticipationRepository | None = None
    if _arena_participation_requested():
        if connector_bundle is None:
            raise RuntimeError(
                "Arena participation requires the authenticated production "
                "control plane"
            )
        arena_participation_dsn = (
            os.getenv("ADX_ARENA_API_DATABASE_URL")
            or os.getenv("ADX_HOSTED_CONTROL_DATABASE_URL")
            or os.getenv("ADX_CONNECTOR_DATABASE_URL")
            or ""
        ).strip()
        arena_participation = PostgresArenaParticipationRepository(
            arena_participation_dsn
        )

    pawnhouse_repository: PostgresPawnhouseRepository | None = None
    pawnhouse_coordinator: PawnhouseHostedCoordinator | None = None
    pawnhouse_coordinator_task: asyncio.Task[None] | None = None
    pawnhouse_orchestrator: PawnhouseGameOrchestrator | None = None
    pawnhouse_orchestrator_task: asyncio.Task[None] | None = None
    settlement_confirmation_reader: EvmJsonRpcConfirmationReader | None = None
    pawnhouse_dev_token = ""
    pawnhouse_dsn = ""

    if pawnhouse_core_enabled:
        pawnhouse_dsn = os.getenv(
            "ADX_ARENA_CORE_DATABASE_URL",
            "",
        ).strip()
        if not pawnhouse_dsn:
            raise RuntimeError(
                "ADX_ARENA_CORE_DATABASE_URL is required when Arena Core is enabled"
            )
        pawnhouse_repository = PostgresPawnhouseRepository(pawnhouse_dsn)

    if pawnhouse_dev_enabled:
        pawnhouse_dev_token = os.getenv(
            "ADX_ARENA_DEV_TOKEN",
            "",
        ).strip()
        assert pawnhouse_repository is not None
        pawnhouse_orchestrator = PawnhouseGameOrchestrator(
            repository=pawnhouse_repository
        )
        settlement_rpc_url = os.getenv(
            "ADX_ARENA_SETTLEMENT_RPC_URL",
            "",
        ).strip()
        if settlement_rpc_url:
            blockscout_url = os.getenv(
                "ADX_ARENA_SETTLEMENT_BLOCKSCOUT_URL",
                "",
            ).strip()
            if (
                os.getenv("ADX_ENV", "development").strip().lower() == "production"
                and (
                    not settlement_rpc_url.lower().startswith("https://")
                    or (
                        blockscout_url
                        and not blockscout_url.lower().startswith("https://")
                    )
                )
            ):
                raise RuntimeError("Production settlement readers must use HTTPS")
            settlement_confirmation_reader = EvmJsonRpcConfirmationReader(
                settlement_rpc_url,
                blockscout_base_url=blockscout_url or None,
            )
        if hosted_bundle is not None and _hosted_local_dev_requested():
            pawnhouse_coordinator = PawnhouseHostedCoordinator(
                pawnhouse=pawnhouse_repository,
                arena_core=PostgresArenaCoreRepository(pawnhouse_dsn),
                worker_id="pawnhouse-coordinator-local-dev",
                lease_seconds=600,
            )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if connector_bundle is not None:
            await connector_bundle.initialize()
        if connector_arena_registrar is not None:
            await connector_arena_registrar.initialize()
        if hosted_bundle is not None:
            await hosted_bundle.initialize()
        if arena_participation is not None:
            await arena_participation.initialize()
        if pawnhouse_repository is not None:
            await pawnhouse_repository.initialize()

        nonlocal pawnhouse_coordinator_task, pawnhouse_orchestrator_task
        if pawnhouse_orchestrator is not None:
            pawnhouse_orchestrator_task = asyncio.create_task(
                pawnhouse_orchestrator.run_forever(poll_seconds=0.1),
                name="pawnhouse-game-orchestrator-local-dev",
            )
        if pawnhouse_coordinator is not None:
            await pawnhouse_coordinator.initialize()
            pawnhouse_coordinator_task = asyncio.create_task(
                pawnhouse_coordinator.run_forever(poll_seconds=0.1),
                name="pawnhouse-hosted-coordinator-local-dev",
            )
        try:
            yield
        finally:
            if pawnhouse_orchestrator is not None:
                pawnhouse_orchestrator.stop()
                if pawnhouse_orchestrator_task is not None:
                    await pawnhouse_orchestrator_task
                    pawnhouse_orchestrator_task = None
            if pawnhouse_coordinator is not None:
                pawnhouse_coordinator.stop()
                if pawnhouse_coordinator_task is not None:
                    await pawnhouse_coordinator_task
                    pawnhouse_coordinator_task = None
                await pawnhouse_coordinator.close()
            if pawnhouse_repository is not None:
                await pawnhouse_repository.close()
            if arena_participation is not None:
                await arena_participation.close()
            if hosted_bundle is not None:
                await hosted_bundle.close()
            if connector_bundle is not None:
                await connector_bundle.close()
            if connector_arena_registrar is not None:
                await connector_arena_registrar.close()

    app = FastAPI(
        title="Arena 402",
        description="Round-based AI trading game control and observation API",
        version="0.3.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(production_connector),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-CSRF-Token",
            "X-Arena-Dev-Token",
        ],
    )

    if connector_bundle is not None:
        app.state.connector_gateway_enabled = True
        app.state.connector_gateway_mode = "production"
        app.state.connector_gateway = connector_bundle.service
        app.state.connector_auth = connector_bundle.auth
        app.include_router(connector_bundle.router)
    else:
        _mount_connector_gateway(app, connector_demo_enabled)
        app.state.connector_gateway_mode = (
            "demo" if app.state.connector_gateway_enabled else "off"
        )

    if hosted_bundle is None:
        hosted_catalog = CapabilityCatalogService(
            CapabilityRegistry(),
            hosted_agents_enabled=False,
            credential_ingress_configured=False,
        )
        app.state.hosted_agents_creation_enabled = False
        app.include_router(create_hosted_agent_router(catalog=hosted_catalog))
    else:
        app.state.hosted_agents_creation_enabled = True
        app.state.hosted_control = hosted_bundle
        app.include_router(
            create_hosted_agent_router(
                catalog=hosted_bundle.catalog,
                auth=hosted_bundle.auth,
                credential_service=hosted_bundle.credential_service,
                agent_service=hosted_bundle.agent_service,
                enable_mutations=True,
            )
        )

    if arena_participation is not None:
        assert connector_bundle is not None
        app.state.arena_participation = arena_participation
        app.include_router(
            create_arena_participation_router(
                auth=connector_bundle.auth,
                repository=arena_participation,
            )
        )

    app.state.pawnhouse_mode = "off"
    if pawnhouse_repository is not None:
        app.state.pawnhouse_repository = pawnhouse_repository
        if connector_bundle is not None:
            app.include_router(
                create_game_operator_router(
                    auth=connector_bundle.auth,
                    repository=pawnhouse_repository,
                )
            )
        if pawnhouse_dev_enabled:
            app.state.pawnhouse_mode = "development"
            app.include_router(
                create_pawnhouse_router(
                    repository=pawnhouse_repository,
                    dev_token=pawnhouse_dev_token,
                    auth=connector_bundle.auth if connector_bundle is not None else None,
                    confirmation_reader=settlement_confirmation_reader,
                )
            )
        else:
            app.state.pawnhouse_mode = (
                "operator" if connector_bundle is not None else "read_only"
            )
            app.include_router(
                create_pawnhouse_read_router(repository=pawnhouse_repository)
            )

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "version": app.version,
            "connector_gateway": app.state.connector_gateway_mode,
            "hosted_agent_creation": app.state.hosted_agents_creation_enabled,
            "arena_participation": arena_participation is not None,
            "pawnhouse": app.state.pawnhouse_mode,
        }

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web.api:create_app", factory=True, reload=True, port=8000)
