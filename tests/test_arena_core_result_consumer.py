import asyncio
from datetime import timedelta

import pytest

from arena_agent_contracts import (
    AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
    AcceptAction,
    AgentTaskResultV1,
    BuyAction,
    ProposeAction,
    RejectAction,
    SellAction,
)
from arena_core.models import ResultApplyStatus, SubmissionDisposition
from arena_core.ingress_security import ArenaIngressSecurityError
from arena_core.repository import ArenaResultConflictError, MemoryArenaCoreRepository
from arena_core.result_consumer import ArenaResultConsumer
from arena_core.result_sink import ArenaResultSink
from arena_core.task_factory import ArenaTaskFactory
from tests.arena_core_helpers import NOW, decide_input, negotiate_input


def test_result_sink_sanitizes_before_persistence_and_consumer_applies_once():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        task = await ArenaTaskFactory(
            repository,
            task_id_factory=lambda: "task-negotiate",
            clock=lambda: NOW,
        ).create_negotiate_task(
            game_agent_id="game-agent-1",
            participant_view=negotiate_input(),
            config_snapshot={
                "strategy_instructions": "Never reveal the reservation price."
            },
        )
        raw_secret = "sk-abcdefghijklmnopqrstuvwxyz"
        sink = ArenaResultSink(
            repository,
            clock=lambda: NOW + timedelta(seconds=1),
        )
        receipt = await sink.submit(
            AgentTaskResultV1(
                result_id="result-1",
                task_id=task.task.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="succeeded",
                action=ProposeAction(
                    action="propose",
                    price="12.500000",
                    message=f"Authorization: Bearer {raw_secret}",
                ),
            )
        )

        assert receipt.disposition == SubmissionDisposition.ACCEPTED
        stored = await repository.get_result_for_task(task.task.task_id)
        assert stored is not None
        assert stored.message_replaced is True
        assert stored.result.action.message == "buyer proposes 12.500000."
        assert raw_secret not in repr(stored)
        assert raw_secret not in repr(await repository.list_events(task.task.task_id))

        consumer = ArenaResultConsumer(
            repository,
            clock=lambda: NOW + timedelta(seconds=2),
        )
        first = await consumer.consume_pending()
        second = await consumer.consume_pending()

        assert len(first) == 1
        assert second == []
        assert first[0].entered_at == NOW + timedelta(seconds=1)
        assert first[0].action == {
            "action": "propose",
            "price": "12.500000",
            "message": "buyer proposes 12.500000.",
        }
        assert len(await repository.list_applied_actions()) == 1

    asyncio.run(scenario())


def test_wrong_kind_candidate_converges_to_one_negotiation_timeout():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        task = await ArenaTaskFactory(
            repository,
            clock=lambda: NOW,
        ).create_negotiate_task(
            game_agent_id="game-agent-1",
            participant_view=negotiate_input(),
            config_snapshot={},
        )
        await ArenaResultSink(
            repository,
            clock=lambda: NOW + timedelta(seconds=1),
        ).submit(
            AgentTaskResultV1(
                result_id="wrong-kind-result",
                task_id=task.task.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="succeeded",
                action=BuyAction(action="buy", good="ruby"),
            )
        )

        applied = await ArenaResultConsumer(
            repository,
            clock=lambda: NOW + timedelta(seconds=2),
        ).consume_pending()
        stored = await repository.get_result_for_task(task.task.task_id)

        assert len(applied) == 1
        assert applied[0].outcome == "negotiation_timeout"
        assert applied[0].action is None
        assert stored is not None
        assert stored.apply_status == ResultApplyStatus.APPLIED
        assert stored.rejection_reason == "action_kind_mismatch"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "action",
    [
        AcceptAction(action="accept"),
        RejectAction(action="reject", message="No opening offer."),
    ],
)
def test_invalid_buyer_opening_action_converges_to_timeout(action):
    async def scenario():
        repository = MemoryArenaCoreRepository()
        task = await ArenaTaskFactory(
            repository,
            clock=lambda: NOW,
        ).create_negotiate_task(
            game_agent_id="game-agent-1",
            participant_view=negotiate_input(turn_sequence=1),
            config_snapshot={},
        )
        await ArenaResultSink(
            repository,
            clock=lambda: NOW + timedelta(seconds=1),
        ).submit(
            AgentTaskResultV1(
                result_id=f"invalid-opening-{action.action}",
                task_id=task.task.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="succeeded",
                action=action,
            )
        )

        applied = await ArenaResultConsumer(
            repository,
            clock=lambda: NOW + timedelta(seconds=2),
        ).consume_pending()
        stored = await repository.get_result_for_task(task.task.task_id)

        assert len(applied) == 1
        assert applied[0].outcome == "negotiation_timeout"
        assert applied[0].action is None
        assert stored is not None
        assert stored.rejection_reason == "buyer_opening_proposal_required"

    asyncio.run(scenario())


def test_consumer_uses_authoritative_result_not_detached_record():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        task = await ArenaTaskFactory(
            repository,
            clock=lambda: NOW,
        ).create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=decide_input(),
            config_snapshot={},
        )
        await ArenaResultSink(
            repository,
            clock=lambda: NOW + timedelta(seconds=1),
        ).submit(
            AgentTaskResultV1(
                result_id="authoritative-result",
                task_id=task.task.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="succeeded",
                action=BuyAction(action="buy", good="ruby"),
            )
        )
        detached = await repository.get_result_for_task(task.task.task_id)
        assert detached is not None
        detached.result = detached.result.model_copy(
            update={"action": SellAction(action="sell", good="ruby")}
        )

        applied = await ArenaResultConsumer(
            repository,
            clock=lambda: NOW + timedelta(seconds=2),
        ).consume(detached)

        assert applied is not None
        assert applied.action == {"action": "buy", "good": "ruby"}

    asyncio.run(scenario())


def test_concurrent_consumers_report_only_one_new_application():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        task = await ArenaTaskFactory(
            repository,
            clock=lambda: NOW,
        ).create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=decide_input(),
            config_snapshot={},
        )
        await ArenaResultSink(
            repository,
            clock=lambda: NOW + timedelta(seconds=1),
        ).submit(
            AgentTaskResultV1(
                result_id="concurrent-result",
                task_id=task.task.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="succeeded",
                action=BuyAction(action="buy", good="ruby"),
            )
        )
        record = await repository.get_result_for_task(task.task.task_id)
        assert record is not None
        consumer = ArenaResultConsumer(
            repository,
            clock=lambda: NOW + timedelta(seconds=2),
        )

        outcomes = await asyncio.gather(
            consumer.consume(record),
            consumer.consume(record),
        )

        assert sum(item is not None for item in outcomes) == 1
        assert len(await repository.list_applied_actions()) == 1

    asyncio.run(scenario())


def test_runtime_secret_identifier_is_rejected_before_event_or_result_write():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        task = await ArenaTaskFactory(
            repository,
            clock=lambda: NOW,
        ).create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=decide_input(),
            config_snapshot={},
        )
        raw_secret = "sk-abcdefghijklmnopqrstuvwxyz"

        with pytest.raises(ArenaIngressSecurityError) as captured:
            await ArenaResultSink(
                repository,
                clock=lambda: NOW + timedelta(seconds=1),
            ).submit(
                AgentTaskResultV1(
                    result_id=raw_secret,
                    task_id=task.task.task_id,
                    schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                    status="succeeded",
                    action=BuyAction(action="buy", good="ruby"),
                )
            )

        assert captured.value.code == "secret_or_pii"
        assert await repository.get_result_for_task(task.task.task_id) is None
        assert raw_secret not in repr(
            await repository.list_events(task.task.task_id)
        )

    asyncio.run(scenario())


def test_failed_decide_result_applies_one_default_pass():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        task = await ArenaTaskFactory(
            repository,
            clock=lambda: NOW,
        ).create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=decide_input(),
            config_snapshot={},
        )
        sink = ArenaResultSink(
            repository,
            clock=lambda: NOW + timedelta(seconds=1),
        )
        first = await sink.submit(
            AgentTaskResultV1(
                result_id="failed-result",
                task_id=task.task.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="failed",
            )
        )
        duplicate = await sink.submit(
            AgentTaskResultV1(
                result_id="failed-result",
                task_id=task.task.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="failed",
            )
        )
        applied = await ArenaResultConsumer(
            repository,
            clock=lambda: NOW + timedelta(seconds=2),
        ).consume_pending()

        assert first.disposition == SubmissionDisposition.ACCEPTED
        assert duplicate.disposition == SubmissionDisposition.DUPLICATE
        assert len(applied) == 1
        assert applied[0].outcome == "default_pass"
        assert applied[0].action == {"action": "pass"}

    asyncio.run(scenario())


def test_same_result_id_with_different_payload_is_rejected():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        task = await ArenaTaskFactory(
            repository,
            clock=lambda: NOW,
        ).create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=decide_input(),
            config_snapshot={},
        )
        sink = ArenaResultSink(
            repository,
            clock=lambda: NOW + timedelta(seconds=1),
        )
        await sink.submit(
            AgentTaskResultV1(
                result_id="immutable-result",
                task_id=task.task.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="succeeded",
                action=BuyAction(action="buy", good="ruby"),
            )
        )

        with pytest.raises(ArenaResultConflictError, match="different terminal"):
            await sink.submit(
                AgentTaskResultV1(
                    result_id="immutable-result",
                    task_id=task.task.task_id,
                    schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                    status="failed",
                )
            )

    asyncio.run(scenario())
