"""Dedicated single-writer Connector/Auth control plane for production.

Device WebSockets are mounted by ``web.connector_wss`` so this process can
retain pairing, auth, Binding and MCP control-plane single-writer semantics.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, PlainTextResponse

from arena_game import PostgresPawnhouseRepository
from arena_core import (
    ArenaResultSink,
    PostgresArenaCoreRepository,
    PostgresConnectorArenaRegistrar,
)
from arena_mcp import (
    ArenaTaskBroker,
    ExecutionTokenCodec,
    create_arena_mcp_router,
)
from connector_gateway import (
    ConnectorArenaTaskNotifier,
    ConnectorArenaTaskDispatcher,
    build_production_connector,
)
from db.schema_identity import verify_repository_schema_identity
from web.api import _allowed_origins
from web.current_game_admin_api import create_current_game_admin_router
from web.metrics import ApiMetrics, ApiMetricsMiddleware, postgres_readiness


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def create_app() -> FastAPI:
    if os.getenv("ADX_ENV", "").strip().lower() != "production":
        raise RuntimeError("Dedicated Connector API requires ADX_ENV=production")

    registrar = PostgresConnectorArenaRegistrar(
        (
            os.getenv("ADX_ARENA_API_DATABASE_URL")
            or os.getenv("ADX_CONNECTOR_DATABASE_URL")
            or ""
        ).strip()
    )
    result_core = PostgresArenaCoreRepository(
        _required("ADX_ARENA_CORE_DATABASE_URL")
    )
    current_game_admin_repository = PostgresPawnhouseRepository(
        _required("ADX_ARENA_CORE_DATABASE_URL")
    )
    bundle = build_production_connector(
        arena_registrar=registrar,
        include_websocket=False,
    )
    result_sink = ArenaResultSink(result_core)
    bundle.service.bind_agent_task_result_sink(result_sink)
    mcp_enabled = os.getenv(
        "ADX_ARENA_MCP_ENABLED",
        "",
    ).strip().lower() in {"1", "true", "yes"}
    dispatcher: ConnectorArenaTaskDispatcher | None = None
    session_notifier: ConnectorArenaTaskNotifier | None = None
    mcp_broker: ArenaTaskBroker | None = None
    mcp_token_codec: ExecutionTokenCodec | None = None
    if mcp_enabled:
        mcp_token_codec = ExecutionTokenCodec(_required("ADX_ARENA_MCP_TOKEN_SECRET"))
        mcp_broker = ArenaTaskBroker(
            repository=result_core,
            result_sink=result_sink,
            gateway=bundle.service,
        )
        # This single-writer control-plane loop creates an idempotent managed
        # Session Command when needed. It owns no socket, so WSS workers run a
        # second notifier with Session creation disabled to send only hints.
        session_notifier = ConnectorArenaTaskNotifier(
            repository=result_core,
            gateway=bundle.service,
        )
    else:
        dispatcher = ConnectorArenaTaskDispatcher(
            repository=result_core,
            gateway=bundle.service,
        )
    dispatcher_task: asyncio.Task[None] | None = None
    session_notifier_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal dispatcher_task
        nonlocal session_notifier_task
        await registrar.initialize()
        await result_core.initialize()
        await current_game_admin_repository.initialize()
        await bundle.initialize()
        await verify_repository_schema_identity(repositories)
        if dispatcher is not None:
            dispatcher_task = asyncio.create_task(
                dispatcher.run_forever(poll_seconds=0.25),
                name="arena-connector-task-dispatcher",
            )
        if session_notifier is not None:
            session_notifier_task = asyncio.create_task(
                session_notifier.run_forever(poll_seconds=0.25),
                name="arena-connector-session-notifier",
            )
        try:
            yield
        finally:
            if session_notifier is not None:
                session_notifier.stop()
                if session_notifier_task is not None:
                    await session_notifier_task
                    session_notifier_task = None
            if dispatcher is not None:
                dispatcher.stop()
                if dispatcher_task is not None:
                    await dispatcher_task
                    dispatcher_task = None
            await bundle.close()
            await current_game_admin_repository.close()
            await registrar.close()
            await result_core.close()

    app = FastAPI(
        title="Arena 402 Connector",
        version="0.3.0",
        lifespan=lifespan,
    )
    metrics = ApiMetrics()
    repositories = {
        "connector": bundle.repository,
        "connector_registrar": registrar,
        "result_sink": result_core,
        "current_game_admin": current_game_admin_repository,
    }
    app.add_middleware(ApiMetricsMiddleware, metrics=metrics)
    allowed_origins = _allowed_origins(True)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=[
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "OPTIONS",
        ],
        allow_headers=[
            "Accept",
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "MCP-Protocol-Version",
            "Mcp-Method",
            "Mcp-Name",
            "X-CSRF-Token",
        ],
    )
    app.include_router(bundle.router)
    admin_subjects = frozenset(
        value.strip()
        for value in os.getenv(
            "ADX_ARENA_ADMIN_GITHUB_SUBJECTS", ""
        ).split(",")
        if value.strip().isdigit()
    )
    app.include_router(
        create_current_game_admin_router(
            auth=bundle.auth,
            repository=current_game_admin_repository,
            github_subjects=admin_subjects,
        )
    )
    if mcp_broker is not None:
        assert mcp_token_codec is not None
        app.include_router(
            create_arena_mcp_router(
                broker=mcp_broker,
                token_codec=mcp_token_codec,
                gateway=bundle.service,
                allowed_origins={origin.rstrip("/") for origin in allowed_origins},
            )
        )

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "version": app.version,
            "connector_gateway": "production",
            "arena_mcp": mcp_enabled,
        }

    @app.get("/api/ready")
    async def ready() -> JSONResponse:
        try:
            dependencies = await postgres_readiness(repositories)
        except (TimeoutError, asyncio.TimeoutError):
            dependencies = {
                name: "unavailable" for name in repositories
            }
        ready_now = all(value == "ok" for value in dependencies.values())
        if not ready_now:
            metrics.readiness_failures += 1
        return JSONResponse(
            {
                "status": "ready" if ready_now else "unavailable",
                "dependencies": dependencies,
            },
            status_code=200 if ready_now else 503,
        )

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> PlainTextResponse:
        return PlainTextResponse(
            metrics.render(repositories),
            media_type="text/plain; version=0.0.4",
        )

    return app


__all__ = ["create_app"]
