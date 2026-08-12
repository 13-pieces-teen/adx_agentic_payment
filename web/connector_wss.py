"""Horizontally scalable Connector WSS data plane for production."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from starlette.responses import JSONResponse, PlainTextResponse

from arena_core import ArenaResultSink, PostgresArenaCoreRepository
from connector_gateway import (
    ConnectorArenaTaskNotifier,
    ConnectorSharedCommandRouter,
    build_production_connector,
    create_connector_websocket_router,
)
from db.schema_identity import verify_repository_schema_identity
from web.metrics import ApiMetrics, ApiMetricsMiddleware, postgres_readiness


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def create_app() -> FastAPI:
    if os.getenv("ADX_ENV", "").strip().lower() != "production":
        raise RuntimeError("Connector WSS workers require ADX_ENV=production")

    bundle = build_production_connector()
    result_core = PostgresArenaCoreRepository(
        _required("ADX_ARENA_CORE_DATABASE_URL")
    )
    bundle.service.bind_agent_task_result_sink(ArenaResultSink(result_core))
    mcp_enabled = os.getenv(
        "ADX_ARENA_MCP_ENABLED",
        "",
    ).strip().lower() in {"1", "true", "yes"}
    command_router = ConnectorSharedCommandRouter(bundle.service)
    notifier = (
        ConnectorArenaTaskNotifier(
            repository=result_core,
            gateway=bundle.service,
            manage_sessions=False,
        )
        if mcp_enabled
        else None
    )
    command_router_task: asyncio.Task[None] | None = None
    notifier_task: asyncio.Task[None] | None = None
    repositories = {
        "connector": bundle.repository,
        "result_sink": result_core,
    }

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal command_router_task
        nonlocal notifier_task
        await result_core.initialize()
        await bundle.service.initialize()
        await verify_repository_schema_identity(repositories)
        command_router_task = asyncio.create_task(
            command_router.run_forever(poll_seconds=0.25),
            name="connector-shared-command-router",
        )
        if notifier is not None:
            notifier_task = asyncio.create_task(
                notifier.run_forever(poll_seconds=0.25),
                name="arena-connector-task-notifier",
            )
        try:
            yield
        finally:
            command_router.stop()
            if notifier is not None:
                notifier.stop()
            if command_router_task is not None:
                await command_router_task
                command_router_task = None
            if notifier_task is not None:
                await notifier_task
                notifier_task = None
            await bundle.service.begin_drain()
            await bundle.close()
            await result_core.close()

    app = FastAPI(
        title="Arena 402 Connector WSS",
        version="0.4.0",
        lifespan=lifespan,
    )
    metrics = ApiMetrics()
    app.add_middleware(ApiMetricsMiddleware, metrics=metrics)
    app.include_router(create_connector_websocket_router(bundle.service))

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "version": app.version,
            "connector_gateway": "wss-worker",
            "instance_id": bundle.service.instance_id,
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
