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
    ArenaResultSink,
    PostgresArenaCoreRepository,
    PostgresArenaParticipationRepository,
    PostgresConnectorArenaRegistrar,
)
from arena_game import (
    EvmJsonRpcConfirmationReader,
    PawnhouseGameOrchestrator,
    PawnhouseAgentRuntimeCoordinator,
    PostgresPawnhouseRepository,
)
from arena_payments.api import create_payment_account_router
from arena_payments.admin_api import create_payment_admin_router
from arena_payments.coordinator import X402SettlementCoordinator
from arena_payments.executor import (
    HttpX402SettlementExecutor,
    X402SettlementExecutor,
)
from arena_payments.facilitator import (
    DisabledFacilitatorClient,
    HttpX402FacilitatorClient,
)
from arena_payments.postgres import PostgresPaymentRepository
from arena_payments.x402_api import create_x402_settlement_router
from arena_wallets import (
    PostgresWalletRepository,
    load_wallet_service_from_env,
)
from connector_gateway import (
    ConnectorArenaTaskDispatcher,
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
from web.ledger_api import create_ledger_router, load_ledger_metadata_from_env
from web.pawnhouse_api import (
    create_pawnhouse_participation_router,
    create_pawnhouse_read_router,
    create_pawnhouse_router,
)
from web.wallet_api import create_wallet_router


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


def _arena_payments_requested() -> bool:
    return os.getenv(
        "ADX_ARENA_PAYMENTS_ENABLED",
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

    payment_repository: PostgresPaymentRepository | None = None
    if _arena_payments_requested():
        if connector_bundle is None:
            raise RuntimeError(
                "Arena payments require the authenticated production "
                "control plane"
            )
        payment_dsn = (
            os.getenv("ADX_ARENA_API_DATABASE_URL")
            or os.getenv("ADX_CONNECTOR_DATABASE_URL")
            or ""
        ).strip()
        if not payment_dsn:
            raise RuntimeError(
                "ADX_ARENA_API_DATABASE_URL is required when payments are enabled"
            )
        payment_repository = PostgresPaymentRepository(payment_dsn)

    wallet_repository: PostgresWalletRepository | None = None
    wallet_service = None
    if connector_bundle is not None:
        wallet_dsn = (
            os.getenv("ADX_ARENA_API_DATABASE_URL")
            or os.getenv("ADX_CONNECTOR_DATABASE_URL")
            or ""
        ).strip()
        if not wallet_dsn:
            raise RuntimeError(
                "ADX_ARENA_API_DATABASE_URL or ADX_CONNECTOR_DATABASE_URL is required "
                "for wallet APIs"
            )
        wallet_repository = PostgresWalletRepository(wallet_dsn)
        wallet_service = load_wallet_service_from_env()

    pawnhouse_repository: PostgresPawnhouseRepository | None = None
    connector_result_core: PostgresArenaCoreRepository | None = None
    connector_task_dispatcher: ConnectorArenaTaskDispatcher | None = None
    connector_task_dispatcher_task: asyncio.Task[None] | None = None
    pawnhouse_coordinator: PawnhouseAgentRuntimeCoordinator | None = None
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
        if connector_bundle is not None:
            connector_result_core = PostgresArenaCoreRepository(pawnhouse_dsn)
            connector_bundle.service.bind_agent_task_result_sink(
                ArenaResultSink(connector_result_core)
            )
            connector_task_dispatcher = ConnectorArenaTaskDispatcher(
                repository=connector_result_core,
                gateway=connector_bundle.service,
            )

    x402_coordinator: X402SettlementExecutor | None = None
    x402_public_api_url = ""
    if payment_repository is not None:
        if pawnhouse_repository is None:
            raise RuntimeError(
                "Arena payments require ADX_ARENA_CORE_ENABLED=true"
            )
        x402_public_api_url = (
            os.getenv("ADX_PUBLIC_API_URL")
            or os.getenv("ADX_GITHUB_OAUTH_CALLBACK_BASE_URL")
            or ""
        ).strip().rstrip("/")
        parsed_payment_url = urlsplit(x402_public_api_url)
        if not parsed_payment_url.scheme or not parsed_payment_url.netloc:
            raise RuntimeError(
                "ADX_PUBLIC_API_URL is required when payments are enabled"
            )
        if environment == "production" and parsed_payment_url.scheme != "https":
            raise RuntimeError(
                "ADX_PUBLIC_API_URL must use HTTPS in production"
            )
        if environment == "production":
            x402_coordinator = HttpX402SettlementExecutor(
                os.getenv("ADX_SETTLEMENT_SERVICE_URL", "").strip(),
                bearer_token=os.getenv(
                    "ADX_SETTLEMENT_SERVICE_TOKEN", ""
                ).strip(),
            )
        else:
            facilitator_url = os.getenv(
                "ADX_X402_FACILITATOR_URL", ""
            ).strip()
            if facilitator_url:
                facilitator = HttpX402FacilitatorClient(
                    facilitator_url,
                    facilitator_id=os.getenv(
                        "ADX_X402_FACILITATOR_ID", "configured"
                    ).strip(),
                    authorization=(
                        os.getenv(
                            "ADX_X402_FACILITATOR_AUTHORIZATION", ""
                        ).strip()
                        or None
                    ),
                )
            else:
                facilitator = DisabledFacilitatorClient()
            x402_coordinator = X402SettlementCoordinator(
                payments=payment_repository,
                arena=pawnhouse_repository,
                facilitator=facilitator,
            )

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
        if connector_bundle is not None:
            pawnhouse_coordinator = PawnhouseAgentRuntimeCoordinator(
                pawnhouse=pawnhouse_repository,
                arena_core=PostgresArenaCoreRepository(pawnhouse_dsn),
                worker_id="pawnhouse-agent-runtime-coordinator-local-dev",
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
        if payment_repository is not None:
            await payment_repository.initialize()
        if wallet_repository is not None:
            await wallet_repository.initialize()
        if pawnhouse_repository is not None:
            await pawnhouse_repository.initialize()
        if connector_result_core is not None:
            await connector_result_core.initialize()

        nonlocal pawnhouse_coordinator_task
        nonlocal pawnhouse_orchestrator_task
        nonlocal connector_task_dispatcher_task
        if connector_task_dispatcher is not None:
            connector_task_dispatcher_task = asyncio.create_task(
                connector_task_dispatcher.run_forever(
                    poll_seconds=0.25
                ),
                name="arena-connector-task-dispatcher",
            )
        if pawnhouse_orchestrator is not None:
            pawnhouse_orchestrator_task = asyncio.create_task(
                pawnhouse_orchestrator.run_forever(poll_seconds=0.1),
                name="pawnhouse-game-orchestrator-local-dev",
            )
        if pawnhouse_coordinator is not None:
            await pawnhouse_coordinator.initialize()
            pawnhouse_coordinator_task = asyncio.create_task(
                pawnhouse_coordinator.run_forever(poll_seconds=0.1),
                name="pawnhouse-agent-runtime-coordinator-local-dev",
            )
        try:
            yield
        finally:
            if connector_task_dispatcher is not None:
                connector_task_dispatcher.stop()
                if connector_task_dispatcher_task is not None:
                    await connector_task_dispatcher_task
                    connector_task_dispatcher_task = None
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
            if payment_repository is not None:
                await payment_repository.close()
            if wallet_repository is not None:
                await wallet_repository.close()
            if hosted_bundle is not None:
                await hosted_bundle.close()
            if connector_bundle is not None:
                await connector_bundle.close()
            if connector_arena_registrar is not None:
                await connector_arena_registrar.close()
            if connector_result_core is not None:
                await connector_result_core.close()

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
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "Last-Event-ID",
            "X-CSRF-Token",
            "X-Arena-Dev-Token",
            "PAYMENT-SIGNATURE",
        ],
        expose_headers=[
            "PAYMENT-REQUIRED",
            "PAYMENT-RESPONSE",
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

    if wallet_repository is not None:
        assert connector_bundle is not None
        assert wallet_service is not None
        app.state.wallet_repository = wallet_repository
        app.include_router(
            create_wallet_router(
                auth=connector_bundle.auth,
                repository=wallet_repository,
                service=wallet_service,
            )
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
                payment_repository=payment_repository,
            )
        )

    if payment_repository is not None:
        assert connector_bundle is not None
        app.state.arena_payments = payment_repository
        app.include_router(
            create_payment_account_router(
                auth=connector_bundle.auth,
                repository=payment_repository,
                wallet_service=wallet_service,
            )
        )
        admin_subjects = frozenset(
            value.strip()
            for value in os.getenv(
                "ADX_ARENA_ADMIN_GITHUB_SUBJECTS", ""
            ).split(",")
            if value.strip().isdigit()
        )
        app.include_router(
            create_payment_admin_router(
                auth=connector_bundle.auth,
                repository=payment_repository,
                github_subjects=admin_subjects,
                facilitator_id=(
                    os.getenv("ADX_X402_FACILITATOR_ID", "").strip()
                    or None
                ),
                signer_mode=(
                    "isolated_settlement_service"
                    if os.getenv("ADX_SETTLEMENT_SERVICE_URL", "").strip()
                    else "disabled"
                ),
            )
        )
        assert pawnhouse_repository is not None
        assert x402_coordinator is not None
        app.include_router(
            create_x402_settlement_router(
                arena=pawnhouse_repository,
                mandates=payment_repository,
                coordinator=x402_coordinator,
                public_api_url=x402_public_api_url,
            )
        )

    app.state.pawnhouse_mode = "off"
    if pawnhouse_repository is not None:
        app.state.pawnhouse_repository = pawnhouse_repository
        app.include_router(
            create_ledger_router(
                repository=pawnhouse_repository,
                metadata=load_ledger_metadata_from_env(),
            )
        )
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
                create_pawnhouse_read_router(
                    repository=pawnhouse_repository,
                    auth=(
                        connector_bundle.auth
                        if connector_bundle is not None
                        else None
                    ),
                )
            )
            if connector_bundle is not None:
                app.include_router(
                    create_pawnhouse_participation_router(
                        repository=pawnhouse_repository,
                        auth=connector_bundle.auth,
                    )
                )

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "version": app.version,
            "connector_gateway": app.state.connector_gateway_mode,
            "hosted_agent_creation": app.state.hosted_agents_creation_enabled,
            "arena_participation": arena_participation is not None,
            "arena_payments": payment_repository is not None,
            "pawnhouse": app.state.pawnhouse_mode,
        }

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web.api:create_app", factory=True, reload=True, port=8000)
