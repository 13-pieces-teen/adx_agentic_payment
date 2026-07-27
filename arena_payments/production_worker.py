"""Dedicated unattended Settlement Worker.

This process is the only Arena 402 backend process that combines mandate
mutation, the narrow wallet signer capability, and Facilitator submission.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone

from arena_game.postgres import PostgresPawnhouseRepository
from arena_game.evm_confirmation import EvmJsonRpcConfirmationReader

from .automatic_worker import AutomaticSettlementWorker
from .coordinator import X402SettlementCoordinator
from .facilitator import build_facilitator_client
from .postgres import PostgresPaymentRepository
from .postgres_worker import PostgresAutomaticSettlementSource
from .signer import HttpWalletSignerClient


_LOGGER = logging.getLogger(__name__)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _https_url(name: str) -> str:
    value = _required(name)
    if not value.lower().startswith("https://"):
        raise RuntimeError(f"{name} must use HTTPS in production")
    return value


class SettlementProductionWorker:
    def __init__(
        self,
        *,
        automatic: AutomaticSettlementWorker,
        payments: PostgresPaymentRepository,
        poll_seconds: float = 0.5,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("settlement_poll_seconds_must_be_positive")
        self._automatic = automatic
        self._payments = payments
        self._poll_seconds = poll_seconds
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._automatic.run_once()
                await self._payments.reconcile_finalized_reservations(
                    now=datetime.now(timezone.utc)
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("settlement_worker_cycle_failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._poll_seconds,
                )
            except TimeoutError:
                pass


async def main() -> None:
    if os.getenv(
        "ADX_ARENA_AUTOMATIC_PAYMENTS_ENABLED", "false"
    ).strip().lower() not in {"1", "true", "yes"}:
        raise RuntimeError(
            "ADX_ARENA_AUTOMATIC_PAYMENTS_ENABLED must be true"
        )
    database_url = _required("ADX_SETTLEMENT_DATABASE_URL")
    payments = PostgresPaymentRepository(database_url)
    arena = PostgresPawnhouseRepository(
        database_url,
        database_role="adx_settlement",
    )
    coordinator = X402SettlementCoordinator(
        payments=payments,
        arena=arena,
        facilitator=build_facilitator_client(os.environ),
    )
    automatic = AutomaticSettlementWorker(
        source=PostgresAutomaticSettlementSource(
            payments=payments,
            arena=arena,
            public_api_url=_https_url("ADX_PUBLIC_API_URL"),
            lease_seconds=int(
                os.getenv("ADX_SETTLEMENT_LEASE_SECONDS", "60")
            ),
            settlement_intent_id=(
                os.getenv("ADX_SETTLEMENT_INTENT_ID", "").strip() or None
            ),
        ),
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
        execution_concurrency=int(
            os.getenv("ADX_SETTLEMENT_WORKER_CONCURRENCY", "4")
        ),
        authorization_recovery_reader=EvmJsonRpcConfirmationReader(
            _https_url("ADX_ARENA_SETTLEMENT_RPC_URL"),
            blockscout_base_url=_https_url(
                "ADX_ARENA_SETTLEMENT_BLOCKSCOUT_URL"
            ),
        ),
    )
    worker = SettlementProductionWorker(
        automatic=automatic,
        payments=payments,
        poll_seconds=float(
            os.getenv("ADX_SETTLEMENT_WORKER_POLL_SECONDS", "0.5")
        ),
    )
    await payments.initialize()
    await arena.initialize()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signal_name, worker.stop)
        except NotImplementedError:
            pass
    try:
        await worker.run_forever()
    finally:
        await arena.close()
        await payments.close()


if __name__ == "__main__":
    asyncio.run(main())
