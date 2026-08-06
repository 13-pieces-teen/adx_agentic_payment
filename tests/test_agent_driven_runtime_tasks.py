"""Real-Runtime task contracts and Result Sink policy for agent_a2a.v1."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from arena_agent_contracts import (
    AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
    AgentTaskResultV1,
    ArenaAgentTaskV1,
    ArenaDecideLimitsV1,
    ArenaInboundRfqV1,
    ArenaMarketDirectoryEntryV1,
    ArenaMarketIntentInputV1,
    ArenaMarketRfqInputV1,
    ArenaMarketSelectInputV1,
    ArenaReputationV1,
)
from arena_core.repository import MemoryArenaCoreRepository
from arena_core.result_consumer import ArenaResultConsumer
from arena_core.task_factory import ArenaTaskFactory
from tests.arena_core_helpers import NOW


DEADLINE = NOW + timedelta(seconds=30)


def _intent_input() -> ArenaMarketIntentInputV1:
    return ArenaMarketIntentInputV1(
        phase="market_intent",
        game_id="game-1",
        round_id="round-1",
        round_index=1,
        cash="20.000000",
        holdings={"grain": 1},
        market={"grain": "2.000000"},
        reputation=ArenaReputationV1(failed_negotiations=0),
        limits=ArenaDecideLimitsV1(
            allowed_actions=["buy", "sell", "pass"],
            allowed_goods=["grain"],
        ),
        deadline_at=DEADLINE,
        market_expires_at=DEADLINE + timedelta(minutes=2),
    )


def _rfq_input() -> ArenaMarketRfqInputV1:
    return ArenaMarketRfqInputV1(
        phase="market_rfq",
        game_id="game-1",
        round_id="round-1",
        round_index=1,
        buyer_intent_id="buyer-intent-1",
        good="grain",
        public_price="1.800000",
        limit_price="2.000000",
        cash="20.000000",
        directory=[
            ArenaMarketDirectoryEntryV1(
                intent_id="seller-intent-1",
                agent_id="seller-agent-1",
                display_name="Seller",
                good="grain",
                public_price="1.900000",
                expires_at=DEADLINE,
            )
        ],
        deadline_at=DEADLINE,
    )


def _select_input() -> ArenaMarketSelectInputV1:
    return ArenaMarketSelectInputV1(
        phase="market_select",
        game_id="game-1",
        round_id="round-1",
        round_index=1,
        seller_intent_id="seller-intent-1",
        good="grain",
        public_price="1.900000",
        limit_price="1.600000",
        inventory_available=1,
        requests=[
            ArenaInboundRfqV1(
                request_id="request-1",
                buyer_agent_id="buyer-agent-1",
                buyer_display_name="Buyer",
                opening_price="1.700000",
                message="希望与你协商。",
                received_at=NOW,
            )
        ],
        deadline_at=DEADLINE,
    )


def test_market_task_kinds_are_strict_and_cannot_be_relabelled() -> None:
    task = ArenaAgentTaskV1.model_validate(
        {
            "taskId": "task-market-intent",
            "kind": "arena.market.intent",
            "schemaVersion": "arena.agent-task.v1",
            "gameId": "game-1",
            "roundId": "round-1",
            "gameAgentId": "game-agent-1",
            "negotiationId": None,
            "deadlineAt": DEADLINE,
            "idempotencyKey": (
                "game-1:round-1:game-agent-1:market-intent"
            ),
            "inputHash": "sha256:" + ("0" * 64),
            "input": _intent_input(),
        }
    )
    assert isinstance(task.input, ArenaMarketIntentInputV1)

    with pytest.raises(ValidationError):
        ArenaAgentTaskV1.model_validate(
            task.model_dump(mode="python")
            | {
                "kind": "arena.decide",
                "idempotency_key": "game-1:round-1:game-agent-1:decide",
            }
        )


def test_factory_creates_idempotent_market_phase_tasks() -> None:
    async def scenario() -> None:
        repository = MemoryArenaCoreRepository()
        factory = ArenaTaskFactory(repository, clock=lambda: NOW)

        intent = await factory.create_market_intent_task(
            game_agent_id="game-agent-1",
            participant_view=_intent_input(),
            config_snapshot={"provider": "fake"},
        )
        rfq = await factory.create_market_rfq_task(
            game_agent_id="game-agent-1",
            participant_view=_rfq_input(),
            config_snapshot={"provider": "fake"},
        )
        select = await factory.create_market_select_task(
            game_agent_id="game-agent-1",
            participant_view=_select_input(),
            config_snapshot={"provider": "fake"},
        )

        assert intent.task.kind == "arena.market.intent"
        assert intent.task.idempotency_key.endswith(":market-intent")
        assert rfq.task.kind == "arena.market.rfq"
        assert rfq.task.idempotency_key.endswith(":market-rfq:1")
        assert select.task.kind == "arena.market.select"
        assert ":market-select:" in select.task.idempotency_key

    asyncio.run(scenario())


def test_each_sequential_rfq_attempt_has_a_distinct_durable_task_key() -> None:
    async def scenario() -> None:
        repository = MemoryArenaCoreRepository()
        factory = ArenaTaskFactory(repository, clock=lambda: NOW)
        first_view = _rfq_input()
        second_payload = first_view.model_dump(
            mode="python",
            by_alias=True,
        )
        second_payload.update(
            {
                "attemptSequence": 2,
                "remainingRfqAttempts": 2,
                "priorAttempts": [
                    {
                        "attemptSequence": 1,
                        "targetIntentId": "seller-intent-old",
                        "status": "rejected",
                    }
                ],
            }
        )
        second_view = ArenaMarketRfqInputV1.model_validate(second_payload)

        first = await factory.create_market_rfq_task(
            game_agent_id="buyer-game-agent",
            participant_view=first_view,
            config_snapshot={"provider": "fake"},
        )
        second = await factory.create_market_rfq_task(
            game_agent_id="buyer-game-agent",
            participant_view=second_view,
            config_snapshot={"provider": "fake"},
        )

        assert first.task.idempotency_key.endswith(":market-rfq:1")
        assert second.task.idempotency_key.endswith(":market-rfq:2")
        assert first.task.task_id != second.task.task_id

    asyncio.run(scenario())


def test_result_sink_applies_agent_authored_market_actions() -> None:
    async def scenario() -> None:
        repository = MemoryArenaCoreRepository()
        factory = ArenaTaskFactory(repository, clock=lambda: NOW)
        cases = (
            (
                await factory.create_market_intent_task(
                    game_agent_id="buyer-game-agent",
                    participant_view=_intent_input(),
                    config_snapshot={"provider": "fake"},
                ),
                {
                    "action": "buy",
                    "good": "grain",
                    "publicPrice": "1.800000",
                    "limitPrice": "2.000000",
                    "message": "希望买入粮草。",
                },
            ),
            (
                await factory.create_market_rfq_task(
                    game_agent_id="buyer-game-agent",
                    participant_view=_rfq_input(),
                    config_snapshot={"provider": "fake"},
                ),
                {
                    "action": "request_negotiations",
                    "requests": [
                        {
                            "targetIntentId": "seller-intent-1",
                            "openingPrice": "1.700000",
                            "message": "选择与你协商。",
                        }
                    ],
                },
            ),
            (
                await factory.create_market_select_task(
                    game_agent_id="seller-game-agent",
                    participant_view=_select_input(),
                    config_snapshot={"provider": "fake"},
                ),
                {"action": "engage", "requestId": "request-1"},
            ),
        )

        for index, (task, action) in enumerate(cases):
            submitted = await repository.submit_result(
                result=AgentTaskResultV1.model_validate(
                    {
                        "resultId": f"runtime-market-{index}",
                        "taskId": task.task.task_id,
                        "schemaVersion": (
                            AGENT_TASK_RESULT_SCHEMA_VERSION_V1
                        ),
                        "status": "succeeded",
                        "action": action,
                    }
                ),
                server_clock=lambda: NOW + timedelta(seconds=1),
                message_replaced=False,
                public_output_policy_version="public-output.v1",
            )
            record = await repository.get_result_for_task(task.task.task_id)
            assert record is not None
            applied = await ArenaResultConsumer(
                repository,
                clock=lambda: NOW + timedelta(seconds=2),
            ).consume(record)

            assert applied is not None
            assert applied.result_id == submitted.authoritative_result_id
            assert applied.kind == task.task.kind
            assert applied.outcome == "candidate"
            assert applied.action == action

    asyncio.run(scenario())


def test_invalid_or_timed_out_market_result_fails_closed() -> None:
    async def scenario() -> None:
        repository = MemoryArenaCoreRepository()
        factory = ArenaTaskFactory(repository, clock=lambda: NOW)
        task = await factory.create_market_rfq_task(
            game_agent_id="buyer-game-agent",
            participant_view=_rfq_input(),
            config_snapshot={"provider": "fake"},
        )
        await repository.submit_result(
            result=AgentTaskResultV1(
                result_id="runtime-market-timeout",
                task_id=task.task.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="timed_out",
            ),
            server_clock=lambda: NOW + timedelta(seconds=1),
            message_replaced=False,
            public_output_policy_version=None,
        )
        record = await repository.get_result_for_task(task.task.task_id)
        assert record is not None

        applied = await ArenaResultConsumer(
            repository,
            clock=lambda: NOW + timedelta(seconds=2),
        ).consume(record)

        assert applied is not None
        assert applied.outcome == "market_timeout"
        assert applied.action is None

    asyncio.run(scenario())


def test_market_price_beyond_atomic_scale_converges_to_default_pass() -> None:
    async def scenario() -> None:
        repository = MemoryArenaCoreRepository()
        task = await ArenaTaskFactory(
            repository,
            clock=lambda: NOW,
        ).create_market_intent_task(
            game_agent_id="buyer-game-agent",
            participant_view=_intent_input(),
            config_snapshot={"provider": "fake"},
        )
        await repository.submit_result(
            result=AgentTaskResultV1.model_validate(
                {
                    "resultId": "runtime-market-overprecise",
                    "taskId": task.task.task_id,
                    "schemaVersion": AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                    "status": "succeeded",
                    "action": {
                        "action": "buy",
                        "good": "grain",
                        "publicPrice": "1.8000001",
                        "limitPrice": "2.0000001",
                        "message": "希望买入粮草。",
                    },
                }
            ),
            server_clock=lambda: NOW + timedelta(seconds=1),
            message_replaced=False,
            public_output_policy_version="public-output.v1",
        )
        record = await repository.get_result_for_task(task.task.task_id)
        assert record is not None

        applied = await ArenaResultConsumer(
            repository,
            clock=lambda: NOW + timedelta(seconds=2),
        ).consume(record)
        stored = await repository.get_result_for_task(task.task.task_id)

        assert applied is not None
        assert applied.outcome == "default_pass"
        assert applied.action == {"action": "pass"}
        assert stored is not None
        assert stored.rejection_reason == "price_precision_exceeded"

    asyncio.run(scenario())
