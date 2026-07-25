"""Production Arena Core, Deadline Finalizer, and settlement recovery worker."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone

from arena_core.postgres_repository import PostgresArenaCoreRepository
from .evm_confirmation import EvmJsonRpcConfirmationReader
from .hosted_coordinator import PawnhouseAgentRuntimeCoordinator
from .orchestrator import PawnhouseGameOrchestrator
from .postgres import PostgresPawnhouseRepository
from .settlement_worker import SettlementRecoveryWorker


_LOGGER = logging.getLogger(__name__)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _https_url(name: str, *, required: bool) -> str | None:
    value = os.getenv(name, "").strip()
    if not value:
        if required:
            raise RuntimeError(f"{name} is required")
        return None
    if not value.lower().startswith("https://"):
        raise RuntimeError(f"{name} must use HTTPS in production")
    return value


class ArenaProductionWorker:
    """Run the four Arena-owned, non-signing durable loops."""

    def __init__(
        self,
        *,
        game_orchestrator: PawnhouseGameOrchestrator,
        coordinator: PawnhouseAgentRuntimeCoordinator,
        arena_core: PostgresArenaCoreRepository,
        settlement_recovery: SettlementRecoveryWorker,
        coordinator_poll_seconds: float = 0.25,
        finalizer_poll_seconds: float = 1.0,
        settlement_poll_seconds: float = 3.0,
        orchestration_poll_seconds: float = 0.25,
    ) -> None:
        if min(
            orchestration_poll_seconds,
            coordinator_poll_seconds,
            finalizer_poll_seconds,
            settlement_poll_seconds,
        ) <= 0:
            raise ValueError("worker poll intervals must be positive")
        self._game_orchestrator = game_orchestrator
        self._coordinator = coordinator
        self._arena_core = arena_core
        self._settlement_recovery = settlement_recovery
        self._coordinator_poll_seconds = coordinator_poll_seconds
        self._finalizer_poll_seconds = finalizer_poll_seconds
        self._settlement_poll_seconds = settlement_poll_seconds
        self._orchestration_poll_seconds = orchestration_poll_seconds
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()
        self._game_orchestrator.stop()
        self._coordinator.stop()

    async def run_forever(self) -> None:
        tasks = [
            asyncio.create_task(
                self._game_orchestrator.run_forever(
                    poll_seconds=self._orchestration_poll_seconds
                ),
                name="arena-game-orchestrator",
            ),
            asyncio.create_task(
                self._coordinator.run_forever(
                    poll_seconds=self._coordinator_poll_seconds
                ),
                name="arena-pawnhouse-coordinator",
            ),
            asyncio.create_task(
                self._finalizer_loop(),
                name="arena-deadline-finalizer",
            ),
            asyncio.create_task(
                self._settlement_loop(),
                name="arena-settlement-recovery",
            ),
        ]
        await self._stopping.wait()
        await asyncio.gather(*tasks)

    async def _finalizer_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._arena_core.finalize_expired(
                    server_clock=lambda: datetime.now(timezone.utc),
                    limit=100,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.error("arena_deadline_finalizer_cycle_failed")
            await self._wait(self._finalizer_poll_seconds)

    async def _settlement_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._settlement_recovery.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.error("arena_settlement_recovery_cycle_failed")
            await self._wait(self._settlement_poll_seconds)

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass


async def main() -> None:
    database_url = _required("ADX_ARENA_CORE_DATABASE_URL")
    rpc_url = _https_url(
        "ADX_ARENA_SETTLEMENT_RPC_URL",
        required=True,
    )
    blockscout_url = _https_url(
        "ADX_ARENA_SETTLEMENT_BLOCKSCOUT_URL",
        required=False,
    )
    pawnhouse = PostgresPawnhouseRepository(database_url)
    arena_core = PostgresArenaCoreRepository(database_url)
    coordinator = PawnhouseAgentRuntimeCoordinator(
        pawnhouse=pawnhouse,
        arena_core=arena_core,
        worker_id=os.getenv("ADX_ARENA_WORKER_ID") or None,
        lease_seconds=int(
            os.getenv("ADX_ARENA_WORKER_LEASE_SECONDS", "600")
        ),
    )
    game_orchestrator = PawnhouseGameOrchestrator(repository=pawnhouse)
    settlement_recovery = SettlementRecoveryWorker(
        repository=pawnhouse,
        confirmation_reader=EvmJsonRpcConfirmationReader(
            rpc_url or "",
            blockscout_base_url=blockscout_url,
        ),
    )
    worker = ArenaProductionWorker(
        game_orchestrator=game_orchestrator,
        coordinator=coordinator,
        arena_core=arena_core,
        settlement_recovery=settlement_recovery,
        coordinator_poll_seconds=float(
            os.getenv("ADX_ARENA_WORKER_POLL_SECONDS", "0.25")
        ),
        finalizer_poll_seconds=float(
            os.getenv("ADX_ARENA_FINALIZER_POLL_SECONDS", "1")
        ),
        settlement_poll_seconds=float(
            os.getenv("ADX_SETTLEMENT_RECOVERY_POLL_SECONDS", "3")
        ),
        orchestration_poll_seconds=float(
            os.getenv("ADX_ARENA_ORCHESTRATION_POLL_SECONDS", "0.25")
        ),
    )
    await pawnhouse.initialize()
    await coordinator.initialize()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signal_name, worker.stop)
        except NotImplementedError:
            pass
    try:
        await worker.run_forever()
    finally:
        await coordinator.close()
        await pawnhouse.close()


if __name__ == "__main__":
    asyncio.run(main())
