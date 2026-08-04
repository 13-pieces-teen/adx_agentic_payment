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


def _decide_context(*, cash_atomic: int, holdings: dict[str, int]):
    return {
        "game_id": "game-1",
        "round_id": "round-1",
        "round_index": 1,
        "cash_atomic": cash_atomic,
        "holdings": holdings,
        "market": {
            "grain": 1_000_000,
            "iron": 2_000_000,
            "warhorse": 8_000_000,
            "gems": 3_000_000,
        },
        "events": [],
        "market_activity": [],
        "deadline_at": datetime.now(timezone.utc) + timedelta(seconds=5),
    }


def test_decide_view_only_advertises_actions_backed_by_frozen_assets():
    buyer = PawnhouseAgentRuntimeCoordinator._decide_view(
        _decide_context(
            cash_atomic=20_000_000,
            holdings={"grain": 0, "iron": 0, "warhorse": 0, "gems": 0},
        )
    )
    assert buyer.limits.allowed_actions == ["buy", "pass"]
    assert buyer.limits.allowed_goods == [
        "grain",
        "iron",
        "warhorse",
        "gems",
    ]

    seller = PawnhouseAgentRuntimeCoordinator._decide_view(
        _decide_context(
            cash_atomic=0,
            holdings={"grain": 2, "iron": 0, "warhorse": 0, "gems": 1},
        )
    )
    assert seller.limits.allowed_actions == ["sell", "pass"]
    assert seller.limits.allowed_goods == ["grain", "gems"]

    empty = PawnhouseAgentRuntimeCoordinator._decide_view(
        _decide_context(
            cash_atomic=0,
            holdings={"grain": 0, "iron": 0, "warhorse": 0, "gems": 0},
        )
    )
    assert empty.limits.allowed_actions == ["pass"]
    assert empty.limits.allowed_goods == []


class _AgentMarketCore:
    def __init__(self) -> None:
        self.applied: list[str] = []

    async def get_results_for_tasks(self, task_ids):
        return {
            task_id: SimpleNamespace(
                result=SimpleNamespace(result_id=f"result:{task_id}")
            )
            for task_id in task_ids
        }

    async def apply_result(self, *, result_id, server_clock):
        assert server_clock().tzinfo is not None
        self.applied.append(result_id)
        return SimpleNamespace(result_id=result_id)

    async def finalize_expired(self, **_):
        raise AssertionError("future tasks must not be finalized")


class _AgentMarketFactory:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    async def _create(self, kind, participant_view):
        self.kinds.append(kind)
        return SimpleNamespace(
            task=SimpleNamespace(
                task_id=f"task:{kind}",
                deadline_at=participant_view.deadline_at,
            )
        )

    async def create_market_intent_task(self, **values):
        return await self._create(
            "arena.market.intent",
            values["participant_view"],
        )

    async def create_market_rfq_task(self, **values):
        return await self._create(
            "arena.market.rfq",
            values["participant_view"],
        )

    async def create_market_select_task(self, **values):
        return await self._create(
            "arena.market.select",
            values["participant_view"],
        )


class _AgentMarketPawnhouse:
    def __init__(self) -> None:
        self.phase = "decide"
        self.stages: list[str] = []
        self.projected: list[str] = []
        self.materialized = False

    async def agent_market_round_phase(self, **_):
        return self.phase

    async def mark_hosted_run_running(self, *, stage, **_):
        self.stages.append(stage)

    async def renew_hosted_run_lease(self, **_):
        return None

    async def hosted_decide_contexts(self, **_):
        context = _decide_context(
            cash_atomic=20_000_000,
            holdings={"grain": 1, "iron": 0, "warhorse": 0, "gems": 0},
        )
        context.update(
            {
                "participant_id": "participant-1",
                "config_snapshot": {"provider": "fake"},
                "action_timeout_ms": 5_000,
            }
        )
        return [context]

    async def agent_market_rfq_contexts(self, **_):
        deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
        return [
            {
                "game_id": "game-1",
                "round_id": "round-1",
                "round_index": 1,
                "deadline_at": deadline,
                "participant_id": "participant-1",
                "buyer_intent_id": "buyer-intent-1",
                "good": "grain",
                "quantity": 1,
                "public_price_atomic": 1_800_000,
                "limit_price_atomic": 2_000_000,
                "cash_atomic": 20_000_000,
                "directory": [
                    {
                        "intent_id": "seller-intent-1",
                        "agent_id": "seller-agent",
                        "display_name": "Seller",
                        "good": "grain",
                        "quantity": 1,
                        "public_price_atomic": 1_900_000,
                        "expires_at": deadline + timedelta(seconds=5),
                    }
                ],
                "events": [],
                "config_snapshot": {"provider": "fake"},
            }
        ]

    async def agent_market_select_contexts(self, **_):
        deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
        return [
            {
                "game_id": "game-1",
                "round_id": "round-1",
                "round_index": 1,
                "deadline_at": deadline,
                "participant_id": "participant-2",
                "seller_intent_id": "seller-intent-1",
                "good": "grain",
                "quantity": 1,
                "public_price_atomic": 1_900_000,
                "limit_price_atomic": 1_500_000,
                "inventory_available": 1,
                "requests": [
                    {
                        "request_id": "request-1",
                        "buyer_agent_id": "buyer-agent",
                        "buyer_display_name": "Buyer",
                        "opening_price_atomic": 1_800_000,
                        "message": "Trade?",
                        "received_at": datetime.now(timezone.utc),
                    }
                ],
                "events": [],
                "config_snapshot": {"provider": "fake"},
            }
        ]

    async def project_agent_market_result(self, *, result_id):
        self.projected.append(result_id)
        return {}

    async def advance_agent_market_stage(
        self,
        *,
        expected_phase,
        next_phase,
        market_stage,
        **_,
    ):
        assert self.phase == expected_phase
        self.phase = next_phase
        assert market_stage in {"rfq", "select"}

    async def materialize_agent_market_engagements(self, **_):
        self.materialized = True
        return []

    async def active_hosted_negotiation_ids(self, **_):
        return []


def test_agent_market_coordinator_runs_all_agent_authored_stages() -> None:
    async def scenario():
        core = _AgentMarketCore()
        pawnhouse = _AgentMarketPawnhouse()
        coordinator = PawnhouseAgentRuntimeCoordinator(
            pawnhouse=pawnhouse,
            arena_core=core,
        )
        factory = _AgentMarketFactory()
        coordinator._factory = factory
        await coordinator._process(
            run_id="run-1",
            game_id="game-1",
            round_id="round-1",
            lease_epoch=1,
            market_protocol="agent_a2a.v1",
        )
        return core, pawnhouse, factory

    core, pawnhouse, factory = asyncio.run(scenario())
    assert factory.kinds == [
        "arena.market.intent",
        "arena.market.rfq",
        "arena.market.select",
    ]
    assert pawnhouse.stages == ["decide", "match", "negotiate"]
    assert pawnhouse.materialized is True
    assert pawnhouse.projected == core.applied == [
        "result:task:arena.market.intent",
        "result:task:arena.market.rfq",
        "result:task:arena.market.select",
    ]
