from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from arena_agent_contracts import (
    AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
    AGENT_TASK_SCHEMA_VERSION_V1,
    AgentTaskResultV1,
    ArenaAgentTaskV1,
    ProposeAction,
)
from arena_core.hashing import sha256_identifier
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
        "round_count": 5,
        "rounds_remaining": 4,
        "cash_atomic": cash_atomic,
        "holdings": holdings,
        "market": {
            "grain": 1_000_000,
            "iron": 2_000_000,
            "warhorse": 8_000_000,
            "gems": 3_000_000,
        },
        "event_implied_final": {
            "grain": 1_100_000,
            "iron": 2_300_000,
            "warhorse": 7_500_000,
            "gems": 3_400_000,
        },
        "events": [],
        "market_activity": [],
        "previous_round_liquidity": None,
        "completed_actions": [],
        "completed_trades": [],
        "failed_negotiations": 0,
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
    assert buyer.model_dump(by_alias=True)["eventImpliedFinal"] == {
        "grain": "1.100000",
        "iron": "2.300000",
        "warhorse": "7.500000",
        "gems": "3.400000",
    }

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


def test_decide_view_links_authoritative_history_and_round_context():
    context = _decide_context(
        cash_atomic=17_500_000,
        holdings={"grain": 1, "iron": 0, "warhorse": 0, "gems": 1},
    )
    context.update(
        {
            "round_id": "round-2",
            "round_index": 2,
            "round_count": 5,
            "rounds_remaining": 3,
            "failed_negotiations": 2,
            "completed_actions": [
                {
                    "round_id": "round-1",
                    "action": {
                        "action": "buy",
                        "good": "grain",
                        "limitPrice": "2.000000",
                    },
                }
            ],
            "completed_trades": [
                {
                    "round_id": "round-1",
                    "negotiation_id": "negotiation-1",
                    "role": "buyer",
                    "good": "grain",
                    "quantity": 1,
                    "price_atomic": 1_750_000,
                }
            ],
            "previous_round_liquidity": {
                "schemaVersion": "arena.market-liquidity.v1",
                "roundId": "round-1",
                "roundIndex": 1,
                "participantCount": 4,
                "intentCount": 3,
                "passCount": 1,
                "oppositeSideCapacity": 1,
                "priceCompatibleCapacity": 1,
                "priceCompatibilityGap": 0,
                "minimumUnmatchedIntentCount": 1,
                "byGood": {
                    "grain": {
                        "buyIntentCount": 1,
                        "sellIntentCount": 1,
                        "oppositeSideCapacity": 1,
                        "priceCompatibleCapacity": 1,
                    },
                    "iron": {
                        "buyIntentCount": 0,
                        "sellIntentCount": 0,
                        "oppositeSideCapacity": 0,
                        "priceCompatibleCapacity": 0,
                    },
                    "warhorse": {
                        "buyIntentCount": 0,
                        "sellIntentCount": 0,
                        "oppositeSideCapacity": 0,
                        "priceCompatibleCapacity": 0,
                    },
                    "gems": {
                        "buyIntentCount": 0,
                        "sellIntentCount": 1,
                        "oppositeSideCapacity": 0,
                        "priceCompatibleCapacity": 0,
                    },
                },
            },
        }
    )

    view = PawnhouseAgentRuntimeCoordinator._decide_view(context)
    wire = view.model_dump(mode="json", by_alias=True)

    assert wire["roundCount"] == 5
    assert wire["roundsRemaining"] == 3
    assert wire["reputation"] == {"failedNegotiations": 2}
    assert wire["completedActions"][0]["roundId"] == "round-1"
    assert wire["completedTrades"] == [
        {
            "roundId": "round-1",
            "negotiationId": "negotiation-1",
            "role": "buyer",
            "good": "grain",
            "quantity": 1,
            "price": "1.750000",
        }
    ]
    assert wire["previousRoundLiquidity"]["roundIndex"] == 1


def test_agent_market_intent_survives_the_full_sequential_protocol_budget():
    context = _decide_context(
        cash_atomic=20_000_000,
        holdings={"grain": 1, "iron": 0, "warhorse": 0, "gems": 0},
    )
    context["action_timeout_ms"] = 5_000

    view = PawnhouseAgentRuntimeCoordinator._market_intent_view(context)

    assert view.market_expires_at - view.deadline_at == timedelta(
        seconds=60
    )


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
                "round_count": 5,
                "rounds_remaining": 4,
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
                        "failed_negotiations": 2,
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
                "round_count": 5,
                "rounds_remaining": 4,
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

    async def agent_market_fallback_rfq_contexts(self, **_):
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


class _BatchNegotiationFactory:
    async def create_negotiate_task(
        self,
        *,
        game_agent_id,
        participant_view,
        **_,
    ):
        task_id = (
            f"task:{participant_view.negotiation_id}:"
            f"{participant_view.turn_sequence}"
        )
        return SimpleNamespace(
            task=ArenaAgentTaskV1(
                task_id=task_id,
                kind="arena.negotiate",
                schema_version=AGENT_TASK_SCHEMA_VERSION_V1,
                game_id=participant_view.game_id,
                round_id=participant_view.round_id,
                game_agent_id=game_agent_id,
                negotiation_id=participant_view.negotiation_id,
                deadline_at=participant_view.deadline_at,
                idempotency_key=(
                    f"{participant_view.game_id}:"
                    f"{participant_view.round_id}:"
                    f"{participant_view.negotiation_id}:"
                    f"{participant_view.turn_sequence}:"
                    f"{game_agent_id}:negotiate"
                ),
                input_hash=sha256_identifier(participant_view),
                input=participant_view,
            )
        )


class _BatchNegotiationCore:
    def __init__(self, *, stagger_results: bool) -> None:
        self.stagger_results = stagger_results
        self.calls: list[list[str]] = []
        self.applied: list[str] = []

    def _result(self, task_id: str):
        action = None
        status = "timed_out"
        if self.stagger_results and "negotiation-fast" in task_id:
            status = "succeeded"
            action = ProposeAction(
                action="propose",
                price=(
                    "1.800000" if task_id.endswith(":1") else "1.900000"
                ),
                message="Continue the negotiation.",
            )
        return SimpleNamespace(
            result=AgentTaskResultV1(
                result_id=f"result:{task_id}",
                task_id=task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status=status,
                action=action,
            )
        )

    async def get_results_for_tasks(self, task_ids):
        task_ids = list(task_ids)
        self.calls.append(task_ids)
        fast_first = "task:negotiation-fast:1"
        fast_second = "task:negotiation-fast:2"
        slow_first = "task:negotiation-slow:1"
        if self.stagger_results and {
            fast_first,
            slow_first,
        }.issubset(task_ids):
            return {fast_first: self._result(fast_first)}
        if self.stagger_results and {
            fast_second,
            slow_first,
        }.issubset(task_ids):
            return {
                fast_second: self._result(fast_second),
                slow_first: self._result(slow_first),
            }
        return {task_id: self._result(task_id) for task_id in task_ids}

    async def apply_result(self, *, result_id, **_):
        self.applied.append(result_id)

    async def finalize_expired(self, **_):
        raise AssertionError("future tasks must not be finalized")


class _BatchNegotiationPawnhouse:
    def __init__(self, *, fast_turns: int) -> None:
        self.fast_turns = fast_turns
        self.completed_turns = {
            "negotiation-fast": 0,
            "negotiation-slow": 0,
        }

    async def agent_market_round_phase(self, **_):
        return "negotiate"

    async def mark_hosted_run_running(self, **_):
        return None

    async def agent_market_select_contexts(self, **_):
        return []

    async def materialize_agent_market_engagements(self, **_):
        return []

    async def active_hosted_negotiation_ids(self, **_):
        return ["negotiation-fast", "negotiation-slow"]

    async def hosted_negotiation_context(self, *, negotiation_id):
        completed = self.completed_turns[negotiation_id]
        max_turns = self.fast_turns if negotiation_id.endswith("fast") else 1
        if completed >= max_turns:
            return None
        turn = completed + 1
        is_seller_turn = turn == 2
        return {
            "game_id": "game-1",
            "round_id": "round-1",
            "round_index": 1,
            "round_count": 5,
            "rounds_remaining": 4,
            "negotiation_id": negotiation_id,
            "participant_id": f"participant:{negotiation_id}:{turn}",
            "role": "seller" if is_seller_turn else "buyer",
            "good": "grain",
            "quantity": 1,
            "limit_price_atomic": (
                1_500_000 if is_seller_turn else 2_000_000
            ),
            "cash_atomic": 20_000_000,
            "inventory_available": 1 if is_seller_turn else 0,
            "counterparty_agent_id": f"agent:{negotiation_id}",
            "counterparty_name": "Counterparty",
            "events": [],
            "history": (
                [
                    {
                        "turn_sequence": 1,
                        "from_role": "buyer",
                        "action": "propose",
                        "price_atomic": 1_800_000,
                        "message": "Continue the negotiation.",
                    }
                ]
                if is_seller_turn
                else []
            ),
            "latest_quote": (
                {
                    "turn_sequence": 1,
                    "from_role": "buyer",
                    "price_atomic": 1_800_000,
                }
                if is_seller_turn
                else None
            ),
            "turn_sequence": turn,
            "remaining_turns": max_turns - completed,
            "deadline_at": datetime.now(timezone.utc) + timedelta(seconds=5),
            "config_snapshot": {"provider": "fake"},
        }

    async def renew_hosted_run_lease(self, **_):
        return None

    async def apply_hosted_negotiation_action(
        self,
        *,
        negotiation_id,
        result_id,
        **_,
    ):
        expected_turn = self.completed_turns[negotiation_id] + 1
        assert result_id.endswith(f":{expected_turn}")
        self.completed_turns[negotiation_id] = expected_turn

    async def agent_market_fallback_rfq_contexts(self, **_):
        return []


def test_agent_market_negotiations_wait_for_results_in_one_batch() -> None:
    async def scenario():
        core = _BatchNegotiationCore(stagger_results=False)
        pawnhouse = _BatchNegotiationPawnhouse(fast_turns=1)
        coordinator = PawnhouseAgentRuntimeCoordinator(
            pawnhouse=pawnhouse,
            arena_core=core,
        )
        coordinator._factory = _BatchNegotiationFactory()
        await coordinator._process(
            run_id="run-1",
            game_id="game-1",
            round_id="round-1",
            lease_epoch=1,
            market_protocol="agent_a2a.v1",
        )
        return core

    core = asyncio.run(scenario())
    assert len(core.calls) == 1
    assert set(core.calls[0]) == {
        "task:negotiation-fast:1",
        "task:negotiation-slow:1",
    }
    assert len(core.applied) == 2


def test_fast_negotiation_advances_without_waiting_for_slower_peer() -> None:
    async def scenario():
        core = _BatchNegotiationCore(stagger_results=True)
        pawnhouse = _BatchNegotiationPawnhouse(fast_turns=2)
        coordinator = PawnhouseAgentRuntimeCoordinator(
            pawnhouse=pawnhouse,
            arena_core=core,
        )
        coordinator._factory = _BatchNegotiationFactory()
        await coordinator._run_agent_market_negotiations(
            ["negotiation-fast", "negotiation-slow"],
            run_id="run-1",
            lease_epoch=1,
        )
        return core, pawnhouse

    core, pawnhouse = asyncio.run(scenario())
    assert any(
        set(call)
        == {
            "task:negotiation-fast:2",
            "task:negotiation-slow:1",
        }
        for call in core.calls
    )
    assert pawnhouse.completed_turns == {
        "negotiation-fast": 2,
        "negotiation-slow": 1,
    }


def test_agent_market_coordinator_dispatches_agent_selected_fallback() -> None:
    class _FallbackPawnhouse(_AgentMarketPawnhouse):
        def __init__(self) -> None:
            super().__init__()
            self.fallback_calls = 0

        async def agent_market_fallback_rfq_contexts(self, **_):
            self.fallback_calls += 1
            if self.fallback_calls > 1:
                return []
            context = (await self.agent_market_rfq_contexts())[0]
            context.update(
                {
                    "attempt_sequence": 2,
                    "remaining_rfq_attempts": 2,
                    "prior_attempts": [
                        {
                            "attempt_sequence": 1,
                            "target_intent_id": "seller-intent-old",
                            "status": "rejected",
                        }
                    ],
                }
            )
            return [context]

    async def scenario():
        core = _AgentMarketCore()
        pawnhouse = _FallbackPawnhouse()
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
        return pawnhouse, factory

    pawnhouse, factory = asyncio.run(scenario())
    assert pawnhouse.fallback_calls == 2
    assert factory.kinds == [
        "arena.market.intent",
        "arena.market.rfq",
        "arena.market.select",
        "arena.market.rfq",
        "arena.market.select",
    ]
