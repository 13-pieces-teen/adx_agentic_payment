from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from arena_game.hosted_coordinator import PawnhouseAgentRuntimeCoordinator


class _BatchCore:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def get_results_for_tasks(self, task_ids):
        self.calls.append(list(task_ids))
        if len(self.calls) == 1:
            return {"task-fast": "fast-result"}
        return {"task-slow": "slow-result"}

    async def finalize_expired(self, **_):
        raise AssertionError("future tasks must not be finalized")


def _task(task_id: str):
    return SimpleNamespace(
        task=SimpleNamespace(
            task_id=task_id,
            deadline_at=datetime.now(timezone.utc) + timedelta(seconds=5),
        )
    )


def test_decide_result_waiting_uses_one_batch_poll_and_yields_fast_first():
    async def scenario():
        core = _BatchCore()
        coordinator = PawnhouseAgentRuntimeCoordinator(
            pawnhouse=object(),
            arena_core=core,
        )
        yielded = []
        async for task, result in coordinator._iter_terminal_results(
            [_task("task-slow"), _task("task-fast")]
        ):
            yielded.append((task.task.task_id, result))
        return core.calls, yielded

    calls, yielded = asyncio.run(scenario())
    assert calls == [
        ["task-slow", "task-fast"],
        ["task-slow"],
    ]
    assert yielded == [
        ("task-fast", "fast-result"),
        ("task-slow", "slow-result"),
    ]
