import asyncio
from datetime import timedelta

import pytest

from arena_agent_contracts import ArenaPublicEventV1
from arena_core.hashing import sha256_identifier
from arena_core.ingress_security import ArenaIngressSecurityError
from arena_core.repository import (
    ArenaIdempotencyConflictError,
    MemoryArenaCoreRepository,
)
from arena_core.task_factory import ArenaTaskFactory
from tests.arena_core_helpers import NOW, decide_input, negotiate_input


def test_task_factory_freezes_snapshot_and_reuses_idempotent_task():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        task_ids = iter(["task-first", "task-retry"])
        factory = ArenaTaskFactory(
            repository,
            task_id_factory=lambda: next(task_ids),
            clock=lambda: NOW,
        )
        view = decide_input()
        retry_view = decide_input()
        config = {
            "provider": "fake",
            "model": "fake-structured-v1",
            "strategy_instructions": "Prefer preserving cash.",
        }

        created = await factory.create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=view,
            config_snapshot=config,
        )
        original_hash = created.task.input_hash
        config["model"] = "mutated-after-create"
        view.holdings["ruby"] = 999

        stored = await repository.get_task("task-first")
        assert stored is not None
        assert stored.task.input.holdings["ruby"] == 1
        assert stored.config_snapshot["model"] == "fake-structured-v1"
        assert stored.task.input_hash == original_hash
        assert stored.config_hash == sha256_identifier(
            {
                "provider": "fake",
                "model": "fake-structured-v1",
                "strategy_instructions": "Prefer preserving cash.",
            }
        )

        repeated = await factory.create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=retry_view,
            config_snapshot={
                "provider": "fake",
                "model": "fake-structured-v1",
                "strategy_instructions": "Prefer preserving cash.",
            },
        )
        assert repeated.task.task_id == "task-first"

    asyncio.run(scenario())


def test_task_factory_rejects_idempotency_reuse_with_changed_snapshot():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        factory = ArenaTaskFactory(
            repository,
            clock=lambda: NOW,
        )
        await factory.create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=decide_input(cash="100.000000"),
            config_snapshot={"provider": "fake"},
        )

        with pytest.raises(ArenaIdempotencyConflictError):
            await factory.create_decide_task(
                game_agent_id="game-agent-1",
                participant_view=decide_input(cash="99.000000"),
                config_snapshot={"provider": "fake"},
            )

    asyncio.run(scenario())


def test_negotiate_turn_sequence_creates_distinct_task_keys():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        factory = ArenaTaskFactory(repository, clock=lambda: NOW)

        first = await factory.create_negotiate_task(
            game_agent_id="game-agent-1",
            participant_view=negotiate_input(turn_sequence=1),
            config_snapshot={"provider": "fake"},
        )
        second = await factory.create_negotiate_task(
            game_agent_id="game-agent-1",
            participant_view=negotiate_input(turn_sequence=2),
            config_snapshot={"provider": "fake"},
        )

        assert first.task.task_id != second.task.task_id
        assert ":1:game-agent-1:negotiate" in first.task.idempotency_key
        assert ":2:game-agent-1:negotiate" in second.task.idempotency_key

    asyncio.run(scenario())


def test_task_factory_rejects_elapsed_deadline():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        factory = ArenaTaskFactory(repository, clock=lambda: NOW)

        with pytest.raises(ValueError, match="deadline must be in the future"):
            await factory.create_decide_task(
                game_agent_id="game-agent-1",
                participant_view=decide_input(deadline=NOW - timedelta(seconds=1)),
                config_snapshot={"provider": "fake"},
            )

    asyncio.run(scenario())


def test_task_factory_reuses_exact_task_after_deadline_for_run_recovery():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        clock = [NOW]
        task_ids = iter(["task-original", "task-never-inserted"])
        factory = ArenaTaskFactory(
            repository,
            task_id_factory=lambda: next(task_ids),
            clock=lambda: clock[0],
        )
        participant_view = decide_input(
            deadline=NOW + timedelta(seconds=5)
        )
        config = {"provider": "fake"}
        created = await factory.create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=participant_view,
            config_snapshot=config,
        )

        clock[0] = NOW + timedelta(seconds=10)
        recovered = await factory.create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=decide_input(
                deadline=NOW + timedelta(seconds=5)
            ),
            config_snapshot={"provider": "fake"},
        )

        assert recovered.task.task_id == created.task.task_id
        assert recovered.task.deadline_at == NOW + timedelta(seconds=5)

    asyncio.run(scenario())


def test_task_factory_freezes_snapshots_before_repository_wait():
    class GatedRepository(MemoryArenaCoreRepository):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def create_task(self, **kwargs):
            self.entered.set()
            await self.release.wait()
            return await super().create_task(**kwargs)

    async def scenario():
        repository = GatedRepository()
        participant = decide_input()
        config = {
            "strategy_instructions": "Buy conservatively.",
            "parameters": {"temperature": "0.2"},
        }
        creation = asyncio.create_task(
            ArenaTaskFactory(
                repository,
                task_id_factory=lambda: "immutable-snapshot",
                clock=lambda: NOW,
            ).create_decide_task(
                game_agent_id="game-agent-1",
                participant_view=participant,
                config_snapshot=config,
            )
        )
        await repository.entered.wait()
        participant.holdings["ruby"] = 999
        config["parameters"]["temperature"] = "0.9"
        repository.release.set()

        record = await creation

        assert record.task.input.holdings["ruby"] == 1
        assert record.config_snapshot["parameters"]["temperature"] == "0.2"
        assert record.task.input_hash == sha256_identifier(record.task.input)
        assert record.config_hash == sha256_identifier(record.config_snapshot)

    asyncio.run(scenario())


def test_repository_rejects_snapshot_hash_mismatch():
    async def scenario():
        source_repository = MemoryArenaCoreRepository()
        record = await ArenaTaskFactory(
            source_repository,
            task_id_factory=lambda: "hash-source",
            clock=lambda: NOW,
        ).create_decide_task(
            game_agent_id="game-agent-1",
            participant_view=decide_input(),
            config_snapshot={"provider": "fake"},
        )
        bad_task = record.task.model_copy(
            update={"input_hash": "sha256:" + ("0" * 64)}
        )

        with pytest.raises(
            ArenaIdempotencyConflictError,
            match="input hash",
        ):
            await MemoryArenaCoreRepository().create_task(
                task=bad_task,
                config_snapshot=record.config_snapshot,
                config_hash=record.config_hash,
                created_at=NOW,
            )

    asyncio.run(scenario())


def test_task_factory_rejects_secret_before_persistence():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        raw_secret = "sk-abcdefghijklmnopqrstuvwxyz"

        with pytest.raises(ArenaIngressSecurityError) as captured:
            await ArenaTaskFactory(
                repository,
                task_id_factory=lambda: "must-not-persist",
                clock=lambda: NOW,
            ).create_decide_task(
                game_agent_id="game-agent-1",
                participant_view=decide_input(),
                config_snapshot={"api_key": raw_secret},
            )

        assert captured.value.code == "secret_bearing_key"
        assert raw_secret not in repr(captured.value)
        assert await repository.get_task("must-not-persist") is None

    asyncio.run(scenario())


def test_task_factory_rejects_secret_in_public_event_snapshot():
    async def scenario():
        repository = MemoryArenaCoreRepository()
        raw_secret = "sk-abcdefghijklmnopqrstuvwxyz"
        participant = decide_input().model_copy(
            update={
                "events": [
                    ArenaPublicEventV1(
                        event_id="event-1",
                        event_type="news",
                        occurred_at=NOW,
                        summary=f"unexpected credential {raw_secret}",
                    )
                ]
            }
        )

        with pytest.raises(ArenaIngressSecurityError) as captured:
            await ArenaTaskFactory(
                repository,
                task_id_factory=lambda: "unsafe-public-snapshot",
                clock=lambda: NOW,
            ).create_decide_task(
                game_agent_id="game-agent-1",
                participant_view=participant,
                config_snapshot={},
            )

        assert captured.value.code == "secret_or_pii"
        assert raw_secret not in repr(captured.value)
        assert await repository.get_task("unsafe-public-snapshot") is None

    asyncio.run(scenario())
