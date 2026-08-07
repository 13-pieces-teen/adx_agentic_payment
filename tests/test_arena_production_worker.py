"""Production worker wiring for durable Arena Result consumption."""

from __future__ import annotations

import asyncio

from arena_game.production_worker import ArenaProductionWorker


class _ResultConsumer:
    def __init__(self) -> None:
        self.called = asyncio.Event()
        self.limits: list[int] = []

    async def consume_pending(self, *, limit: int):
        self.limits.append(limit)
        self.called.set()
        return []


def test_production_worker_consumes_pending_results_durably() -> None:
    async def scenario() -> None:
        consumer = _ResultConsumer()
        worker = ArenaProductionWorker(
            game_orchestrator=None,  # type: ignore[arg-type]
            coordinator=None,  # type: ignore[arg-type]
            arena_core=None,  # type: ignore[arg-type]
            result_consumer=consumer,  # type: ignore[arg-type]
            settlement_recovery=None,  # type: ignore[arg-type]
            current_game_lifecycle=None,  # type: ignore[arg-type]
            official_agent_filler=None,  # type: ignore[arg-type]
            agent_market_projection=None,  # type: ignore[arg-type]
            result_consumer_poll_seconds=0.01,
        )

        loop = asyncio.create_task(worker._result_consumer_loop())
        await asyncio.wait_for(consumer.called.wait(), timeout=1)
        worker._stopping.set()
        await asyncio.wait_for(loop, timeout=1)

        assert consumer.limits == [100]

    asyncio.run(scenario())
