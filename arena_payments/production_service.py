"""ASGI composition root for the dedicated Settlement Worker."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from arena_game.postgres import PostgresPawnhouseRepository
from arena_game.evm_confirmation import EvmJsonRpcConfirmationReader

from .automatic_worker import AutomaticSettlementWorker
from .coordinator import X402SettlementCoordinator
from .facilitator import HttpX402FacilitatorClient
from .internal_api import create_internal_settlement_router
from .postgres import PostgresPaymentRepository
from .postgres_worker import PostgresAutomaticSettlementSource
from .production_worker import (
    SettlementProductionWorker,
    _https_url,
    _required,
)
from .signer import HttpWalletSignerClient


def create_app() -> FastAPI:
    if os.getenv(
        "ADX_ARENA_AUTOMATIC_PAYMENTS_ENABLED", "false"
    ).strip().lower() not in {"1", "true", "yes"}:
        raise RuntimeError(
            "ADX_ARENA_AUTOMATIC_PAYMENTS_ENABLED must be true"
        )
    database_url = _required("ADX_SETTLEMENT_DATABASE_URL")
    payments = PostgresPaymentRepository(database_url)
    arena = PostgresPawnhouseRepository(database_url)
    source = PostgresAutomaticSettlementSource(
        payments=payments,
        arena=arena,
        public_api_url=_https_url("ADX_PUBLIC_API_URL"),
        lease_seconds=int(os.getenv("ADX_SETTLEMENT_LEASE_SECONDS", "60")),
    )
    coordinator = X402SettlementCoordinator(
        payments=payments,
        arena=arena,
        facilitator=HttpX402FacilitatorClient(
            _required("ADX_X402_FACILITATOR_URL"),
            facilitator_id=_required("ADX_X402_FACILITATOR_ID"),
            authorization=(
                os.getenv("ADX_X402_FACILITATOR_AUTHORIZATION", "").strip()
                or None
            ),
        ),
    )
    worker = SettlementProductionWorker(
        automatic=AutomaticSettlementWorker(
            source=source,
            payments=payments,
            signer=HttpWalletSignerClient(
                _required("ADX_WALLET_SIGNER_URL"),
                bearer_token=_required("ADX_WALLET_SIGNER_TOKEN"),
            ),
            coordinator=coordinator,
            worker_id=(
                os.getenv("ADX_SETTLEMENT_WORKER_ID")
                or "arena402-settlement-worker"
            ),
            authorization_recovery_reader=EvmJsonRpcConfirmationReader(
                _https_url("ADX_ARENA_SETTLEMENT_RPC_URL")
            ),
        ),
        payments=payments,
        poll_seconds=float(
            os.getenv("ADX_SETTLEMENT_WORKER_POLL_SECONDS", "0.5")
        ),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await payments.initialize()
        await arena.initialize()
        task = asyncio.create_task(
            worker.run_forever(),
            name="arena402-settlement-worker",
        )
        try:
            yield
        finally:
            worker.stop()
            await task
            await arena.close()
            await payments.close()

    app = FastAPI(
        title="Arena 402 Settlement",
        version="2.0.0",
        lifespan=lifespan,
    )
    app.include_router(
        create_internal_settlement_router(
            source=source,
            coordinator=coordinator,
            bearer_token=_required("ADX_SETTLEMENT_SERVICE_TOKEN"),
        )
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


__all__ = ["create_app"]
