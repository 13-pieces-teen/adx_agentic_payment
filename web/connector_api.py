"""Dedicated single-worker Connector/Auth ingress for production.

Keeping WebSocket ownership and Connector task dispatch in this process lets
the main HTTP API run multiple stateless workers without splitting the
process-local connection registry.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, PlainTextResponse

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
    bundle = build_production_connector(arena_registrar=registrar)
    result_sink = ArenaResultSink(result_core)
    bundle.service.bind_agent_task_result_sink(result_sink)
    mcp_enabled = os.getenv(
        "ADX_ARENA_MCP_ENABLED",
        "",
    ).strip().lower() in {"1", "true", "yes"}
    dispatcher: ConnectorArenaTaskDispatcher | None = None
    notifier: ConnectorArenaTaskNotifier | None = None
    mcp_broker: ArenaTaskBroker | None = None
    mcp_token_codec: ExecutionTokenCodec | None = None
    if mcp_enabled:
        mcp_token_codec = ExecutionTokenCodec(_required("ADX_ARENA_MCP_TOKEN_SECRET"))
        mcp_broker = ArenaTaskBroker(
            repository=result_core,
            result_sink=result_sink,
            gateway=bundle.service,
        )
        notifier = ConnectorArenaTaskNotifier(
            repository=result_core,
            gateway=bundle.service,
        )
    else:
        dispatcher = ConnectorArenaTaskDispatcher(
            repository=result_core,
            gateway=bundle.service,
        )
    dispatcher_task: asyncio.Task[None] | None = None
    notifier_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal dispatcher_task
        nonlocal notifier_task
        await registrar.initialize()
        await result_core.initialize()
        await bundle.initialize()
        await verify_repository_schema_identity(repositories)
        if dispatcher is not None:
            dispatcher_task = asyncio.create_task(
                dispatcher.run_forever(poll_seconds=0.25),
                name="arena-connector-task-dispatcher",
            )
        if notifier is not None:
            notifier_task = asyncio.create_task(
                notifier.run_forever(poll_seconds=0.25),
                name="arena-connector-task-notifier",
            )
        try:
            yield
        finally:
            if dispatcher is not None:
                dispatcher.stop()
                if dispatcher_task is not None:
                    await dispatcher_task
                    dispatcher_task = None
            if notifier is not None:
                notifier.stop()
                if notifier_task is not None:
                    await notifier_task
                    notifier_task = None
            await bundle.close()
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
    }
    app.add_middleware(ApiMetricsMiddleware, metrics=metrics)
    allowed_origins = _allowed_origins(True)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
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
