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
from connector_gateway import (
    ConnectorArenaTaskDispatcher,
    build_production_connector,
)
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
    bundle.service.bind_agent_task_result_sink(ArenaResultSink(result_core))
    dispatcher = ConnectorArenaTaskDispatcher(
        repository=result_core,
        gateway=bundle.service,
    )
    dispatcher_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal dispatcher_task
        await registrar.initialize()
        await result_core.initialize()
        await bundle.initialize()
        dispatcher_task = asyncio.create_task(
            dispatcher.run_forever(poll_seconds=0.25),
            name="arena-connector-task-dispatcher",
        )
        try:
            yield
        finally:
            dispatcher.stop()
            if dispatcher_task is not None:
                await dispatcher_task
                dispatcher_task = None
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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(True),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-CSRF-Token",
        ],
    )
    app.include_router(bundle.router)

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "version": app.version,
            "connector_gateway": "production",
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
