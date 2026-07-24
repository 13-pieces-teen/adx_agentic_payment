import asyncio
import hashlib
from datetime import timedelta

from arena_agent_contracts import (
    AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
    AgentTaskResultV1,
    BuyAction,
)
from arena_core.finalizer import ArenaDeadlineFinalizer
from arena_core.models import SubmissionDisposition, TaskStatus
from arena_core.repository import MemoryArenaCoreRepository
from arena_core.result_consumer import ArenaResultConsumer
from arena_core.result_sink import ArenaResultSink
from arena_core.task_factory import ArenaTaskFactory
from tests.arena_core_helpers import NOW, decide_input


def test_finalizer_defaults_expired_task_once_and_consumer_applies_pass():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        task = await ArenaTaskFactory(
            repository,
            task_id_factory=lambda: "expired-task",
            clock=lambda: NOW,
        ).create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=decide_input(deadline=NOW + timedelta(seconds=1)),
            config_snapshot={},
        )
        finalizer = ArenaDeadlineFinalizer(
            repository,
            clock=lambda: NOW + timedelta(seconds=2),
        )

        first = await finalizer.finalize_expired()
        second = await finalizer.finalize_expired()
        applied = await ArenaResultConsumer(
            repository,
            clock=lambda: NOW + timedelta(seconds=3),
        ).consume_pending()
        stored_task = await repository.get_task(task.task.task_id)

        assert len(first) == 1
        assert second == []
        assert stored_task is not None
        assert stored_task.status == TaskStatus.DEFAULTED
        assert len(applied) == 1
        assert applied[0].outcome == "default_pass"

    asyncio.run(scenario())


def test_late_submission_cannot_beat_deadline_without_finalizer_tick():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        task = await ArenaTaskFactory(
            repository,
            clock=lambda: NOW,
        ).create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=decide_input(deadline=NOW + timedelta(seconds=1)),
            config_snapshot={},
        )
        late = await ArenaResultSink(
            repository,
            clock=lambda: NOW + timedelta(seconds=2),
        ).submit(
            AgentTaskResultV1(
                result_id="late-candidate",
                task_id=task.task.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="succeeded",
                action=BuyAction(action="buy", good="ruby"),
            )
        )
        result = await repository.get_result_for_task(task.task.task_id)
        events = await repository.list_events(task.task.task_id)

        assert late.disposition == SubmissionDisposition.LATE
        assert result is not None
        expected_id = (
            "default:"
            + hashlib.sha256(task.task.task_id.encode("utf-8")).hexdigest()
        )
        assert result.result.result_id == expected_id
        assert result.result.status == "timed_out"
        assert any(event.event_type == "late_result_ignored" for event in events)
        assert "late-candidate" not in repr(events)

    asyncio.run(scenario())


def test_maximum_length_task_id_can_still_be_finalized():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        task = await ArenaTaskFactory(
            repository,
            task_id_factory=lambda: "t" * 256,
            clock=lambda: NOW,
        ).create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=decide_input(deadline=NOW + timedelta(seconds=1)),
            config_snapshot={},
        )

        records = await ArenaDeadlineFinalizer(
            repository,
            clock=lambda: NOW + timedelta(seconds=2),
        ).finalize_expired()
        repeated = await ArenaDeadlineFinalizer(
            repository,
            clock=lambda: NOW + timedelta(seconds=3),
        ).finalize_expired()

        assert len(records) == 1
        assert repeated == []
        assert records[0].result.task_id == task.task.task_id
        assert len(records[0].result.result_id) <= 256

    asyncio.run(scenario())


def test_submission_clock_is_evaluated_after_repository_wait():
    class GatedRepository(MemoryArenaCoreRepository):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def submit_result(self, **kwargs):
            self.entered.set()
            await self.release.wait()
            return await super().submit_result(**kwargs)

    async def scenario():
        repository = GatedRepository()
        task = await ArenaTaskFactory(
            repository,
            clock=lambda: NOW,
        ).create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=decide_input(deadline=NOW + timedelta(seconds=1)),
            config_snapshot={},
        )
        current = [NOW + timedelta(milliseconds=900)]
        submission = asyncio.create_task(
            ArenaResultSink(
                repository,
                clock=lambda: current[0],
            ).submit(
                AgentTaskResultV1(
                    result_id="cross-deadline",
                    task_id=task.task.task_id,
                    schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                    status="succeeded",
                    action=BuyAction(action="buy", good="ruby"),
                )
            )
        )
        await repository.entered.wait()
        current[0] = NOW + timedelta(seconds=5)
        repository.release.set()

        receipt = await submission
        stored = await repository.get_result_for_task(task.task.task_id)

        assert receipt.disposition == SubmissionDisposition.LATE
        assert stored is not None
        assert stored.result.status == "timed_out"

    asyncio.run(scenario())


def test_result_sink_and_finalizer_race_has_one_terminal_result():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        task = await ArenaTaskFactory(
            repository,
            clock=lambda: NOW,
        ).create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=decide_input(deadline=NOW + timedelta(seconds=1)),
            config_snapshot={},
        )
        sink = ArenaResultSink(
            repository,
            clock=lambda: NOW + timedelta(milliseconds=900),
        )
        finalizer = ArenaDeadlineFinalizer(
            repository,
            clock=lambda: NOW + timedelta(seconds=2),
        )

        submission, finalized = await asyncio.gather(
            sink.submit(
                AgentTaskResultV1(
                    result_id="race-candidate",
                    task_id=task.task.task_id,
                    schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                    status="succeeded",
                    action=BuyAction(action="buy", good="ruby"),
                )
            ),
            finalizer.finalize_expired(),
        )
        result = await repository.get_result_for_task(task.task.task_id)

        assert result is not None
        assert submission.disposition in {
            SubmissionDisposition.ACCEPTED,
            SubmissionDisposition.LATE,
        }
        assert len(finalized) in {0, 1}
        assert len(await repository.pending_results(limit=10)) == 1

    asyncio.run(scenario())
