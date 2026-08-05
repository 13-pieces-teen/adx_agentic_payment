from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic_ai import models
from pydantic_ai.models.test import TestModel

from arena_agent_contracts import (
    AGENT_TASK_SCHEMA_VERSION_V1,
    ArenaAgentTaskV1,
)
from arena_core import (
    PostgresArenaCoreRepository,
    PostgresArenaParticipationRepository,
)
from arena_core.hashing import sha256_identifier, sha256_text_identifier
from hosted_agent_control_plane import (
    CredentialIngressRequest,
    CredentialIngressService,
    HostedAgentCreateRequest,
    HostedAgentService,
    HostedAgentUpdateRequest,
    PostgresHostedAgentControlRepository,
)
from hosted_agent_runtime import MemorySecretStore
from hosted_agent_runtime.capabilities import CapabilityRegistry
from hosted_agent_runtime.postgres_worker import (
    DurableHostedWorker,
    PostgresHostedWorkerRepository,
)
from hosted_agent_runtime.production_providers import (
    ProductionProviderBundle,
    build_production_capability_registry,
)
from hosted_agent_runtime.providers import (
    ProviderInvocationError,
    ProviderResponse,
    ProviderUsage,
)
from tests.arena_core_helpers import decide_input


ADMIN_URL = os.getenv("ADX_TEST_POSTGRES_ADMIN_URL")
API_URL = os.getenv("ADX_TEST_POSTGRES_API_URL")
WORKER_URL = os.getenv("ADX_TEST_POSTGRES_WORKER_URL")
pytestmark = pytest.mark.skipif(
    not (ADMIN_URL and API_URL and WORKER_URL),
    reason="real PostgreSQL integration URLs are not configured",
)
models.ALLOW_MODEL_REQUESTS = False


class _ValidationAdapter:
    adapter_id = "deepseek-openai-chat-v1"

    async def invoke(self, *_: object) -> ProviderResponse:
        return ProviderResponse(
            structured_output={"ok": True},
            usage=ProviderUsage(
                input_tokens=3,
                output_tokens=1,
                cached_input_tokens=0,
                reasoning_tokens=0,
                complete=True,
            ),
        )


class _FailingValidationAdapter:
    adapter_id = "deepseek-openai-chat-v1"

    async def invoke(self, *_: object) -> ProviderResponse:
        raise ProviderInvocationError("authentication_failed")


class _BuiltTestModel:
    def __init__(self) -> None:
        self.model = TestModel(
            custom_output_args={
                "action": {"action": "pass"},
                "decision_summary": {
                    "plan": "Wait for a stronger legal opportunity.",
                    "factors": ["The current edge is insufficient."],
                    "confidence_bps": 7000,
                },
                "memory_patch": {
                    "round_summary": "Reviewed the frozen game state.",
                    "next_plan": "Re-evaluate after the next event.",
                    "observations": ["No legal action has enough edge."],
                    "strategy_adjustments": [],
                    "risk_budget_bps": 4500,
                },
            }
        )
        self.settings = None
        self.resolved = SimpleNamespace(
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
            thinking_enabled=False,
        )

    async def close(self) -> None:
        return None


class _TestModelFactory:
    def build(self, **_: object) -> _BuiltTestModel:
        return _BuiltTestModel()


def test_create_validate_and_join_survive_process_boundaries() -> None:
    async def scenario() -> None:
        import asyncpg

        suffix = uuid.uuid4().hex[:12]
        owner_id = f"user-{suffix}"
        admin = await asyncpg.connect(ADMIN_URL)
        control = PostgresHostedAgentControlRepository(API_URL)
        worker_repository = PostgresHostedWorkerRepository(WORKER_URL)
        participation = PostgresArenaParticipationRepository(API_URL)
        core = PostgresArenaCoreRepository(ADMIN_URL)
        secrets = MemorySecretStore.for_testing().ports
        try:
            await admin.execute(
                """
                INSERT INTO connector_users (
                    user_id, username, password_hash, temporary
                )
                VALUES ($1, $2, 'integration-only-hash', FALSE)
                """,
                owner_id,
                f"test_{suffix}",
            )
            await control.initialize()
            credential_service = CredentialIngressService(
                control,
                secret_writer=secrets.writer,
                fingerprint_pepper=b"p" * 32,
                fingerprint_pepper_version=1,
            )
            credential = await credential_service.create_credential(
                owner_user_id=owner_id,
                request=CredentialIngressRequest(
                    provider_id="deepseek",
                    api_key="integration-provider-key",
                    idempotency_key=f"credential-{suffix}",
                ),
            )
            assert credential.status.value == "stored"

            agent_service = HostedAgentService(
                control,
                capabilities=build_production_capability_registry(),
                hosted_agents_enabled=True,
            )
            created = await agent_service.create_hosted_agent(
                owner_user_id=owner_id,
                request=HostedAgentCreateRequest(
                    display_name="Integration Agent",
                    credential_id=credential.credential_id,
                    provider_id="deepseek",
                    model_id="deepseek-v4-flash",
                    thinking_enabled=True,
                    strategy_instructions="Prefer bounded, valid actions.",
                    idempotency_key=f"agent-{suffix}",
                ),
            )
            assert created.provisioning_status.value == "provisioning"

            await worker_repository.initialize()
            worker = DurableHostedWorker(
                repository=worker_repository,
                providers=ProductionProviderBundle(
                    registry=CapabilityRegistry(),
                    adapters={"deepseek": _ValidationAdapter()},
                ),
                secret_reader=secrets.reader,
                worker_id=f"worker-{suffix}",
            )
            assert await worker.run_once() == 1

            ready = await agent_service.get_hosted_agent(
                owner_user_id=owner_id,
                agent_id=created.agent_id,
            )
            assert ready.provisioning_status.value == "ready"
            assert ready.route_status.value == "ready"

            updating = await agent_service.update_hosted_agent(
                owner_user_id=owner_id,
                agent_id=created.agent_id,
                request=HostedAgentUpdateRequest(
                    provider_id="deepseek",
                    model_id="deepseek-v4-flash",
                    thinking_enabled=False,
                    strategy_instructions=(
                        "Buy iron. Propose 7.000000 and accept at or below it."
                    ),
                    idempotency_key=f"agent-update-{suffix}",
                ),
            )
            assert updating.provisioning_status.value == "provisioning"
            assert updating.route_status.value == "provisioning"
            assert await worker.run_once() == 1

            updated = await agent_service.get_hosted_agent(
                owner_user_id=owner_id,
                agent_id=created.agent_id,
            )
            assert updated.provisioning_status.value == "ready"
            assert updated.route_status.value == "ready"
            assert updated.strategy_instructions.startswith("Buy iron.")

            failing_update = await agent_service.update_hosted_agent(
                owner_user_id=owner_id,
                agent_id=created.agent_id,
                request=HostedAgentUpdateRequest(
                    provider_id="deepseek",
                    model_id="deepseek-v4-flash",
                    thinking_enabled=False,
                    strategy_instructions="This candidate must not be applied.",
                    idempotency_key=f"agent-update-failing-{suffix}",
                ),
            )
            assert failing_update.provisioning_status.value == "provisioning"
            failing_worker = DurableHostedWorker(
                repository=worker_repository,
                providers=ProductionProviderBundle(
                    registry=CapabilityRegistry(),
                    adapters={"deepseek": _FailingValidationAdapter()},
                ),
                secret_reader=secrets.reader,
                worker_id=f"failing-worker-{suffix}",
            )
            assert await failing_worker.run_once() == 1

            preserved = await agent_service.get_hosted_agent(
                owner_user_id=owner_id,
                agent_id=created.agent_id,
            )
            assert preserved.provisioning_status.value == "ready"
            assert preserved.route_status.value == "ready"
            assert preserved.strategy_instructions.startswith("Buy iron.")
            preserved_credential = await credential_service.get_credential(
                owner_user_id=owner_id,
                credential_id=credential.credential_id,
            )
            assert preserved_credential.status.value == "valid"

            game_id = f"game-{suffix}"
            await admin.execute(
                """
                INSERT INTO arena402.games (
                    game_id,
                    round_count,
                    action_timeout_ms,
                    min_participants,
                    max_participants,
                    config_snapshot,
                    event_seed,
                    event_schedule_commitment,
                    market_protocol
                )
                VALUES (
                    $1,
                    1,
                    30000,
                    2,
                    2,
                    '{"initial_cash_atomic":1000000,'
                    '"initial_inventory":{"grain":1,"iron":1,'
                    '"warhorse":1,"gems":1},'
                    '"marketProtocol":"fcfs.v1"}'::jsonb,
                    'integration-event-seed',
                    $2,
                    'fcfs.v1'
                )
                """,
                game_id,
                sha256_text_identifier(
                    f"integration-event-seed:{game_id}"
                ),
            )
            await admin.execute(
                """
                INSERT INTO arena402.game_goods (
                    game_id,
                    good_id,
                    display_name,
                    initial_price_atomic,
                    price_decimal_places
                )
                SELECT $1, good_id, display_name, initial_price, 6
                FROM (
                    VALUES
                        ('grain', 'Grain', 1000000::NUMERIC),
                        ('iron', 'Iron', 2000000::NUMERIC),
                        ('warhorse', 'Warhorse', 3000000::NUMERIC),
                        ('gems', 'Gems', 4000000::NUMERIC)
                ) AS goods(good_id, display_name, initial_price)
                """,
                game_id,
            )
            await admin.execute(
                """
                INSERT INTO games (
                    game_id, status, action_timeout_ms, config_snapshot
                )
                VALUES (
                    $1, 'open', 30000,
                    '{"initial_cash_atomic":1000000,'
                    '"initial_inventory":{"grain":1,"iron":1,'
                    '"warhorse":1,"gems":1}}'::jsonb
                )
                """,
                game_id,
            )
            await participation.initialize()
            request_digest = sha256_identifier(
                {"agentId": created.agent_id, "gameId": game_id}
            )
            joined = await participation.join(
                owner_user_id=owner_id,
                game_id=game_id,
                agent_id=created.agent_id,
                key_digest=sha256_text_identifier(f"join-a-{suffix}"),
                request_digest=request_digest,
            )
            replay = await participation.join(
                owner_user_id=owner_id,
                game_id=game_id,
                agent_id=created.agent_id,
                key_digest=sha256_text_identifier(f"join-b-{suffix}"),
                request_digest=request_digest,
            )
            assert replay == joined
            assert len(await participation.list_for_owner(owner_id)) == 1
            frozen = await admin.fetchrow(
                """
                SELECT
                    game_agent.hosted_strategy_revision_id,
                    memory.strategy_revision_id,
                    memory.memory_version
                FROM game_agents AS game_agent
                JOIN hosted_agent_game_memory AS memory
                  ON memory.game_agent_id = game_agent.game_agent_id
                WHERE game_agent.game_agent_id = $1
                """,
                joined.game_agent_id,
            )
            assert frozen is not None
            assert frozen["hosted_strategy_revision_id"] is not None
            assert (
                frozen["strategy_revision_id"]
                == frozen["hosted_strategy_revision_id"]
            )
            assert frozen["memory_version"] == 0

            config_snapshot_value = await admin.fetchval(
                """
                SELECT config_snapshot
                FROM game_agents
                WHERE game_agent_id = $1
                """,
                joined.game_agent_id,
            )
            config_snapshot = (
                json.loads(config_snapshot_value)
                if isinstance(config_snapshot_value, str)
                else dict(config_snapshot_value)
            )
            deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
            round_id = f"{game_id}:round:1"
            await admin.execute(
                """
                INSERT INTO rounds (
                    round_id,
                    game_id,
                    round_index,
                    phase,
                    deadline_at
                )
                VALUES ($1, $2, 1, 'decide', $3)
                """,
                round_id,
                game_id,
                deadline,
            )
            task_input = decide_input(deadline=deadline).model_copy(
                update={
                    "game_id": game_id,
                    "round_id": round_id,
                }
            )
            task = ArenaAgentTaskV1(
                task_id=f"task-{suffix}",
                kind="arena.decide",
                schema_version=AGENT_TASK_SCHEMA_VERSION_V1,
                game_id=game_id,
                round_id=task_input.round_id,
                game_agent_id=joined.game_agent_id,
                deadline_at=deadline,
                idempotency_key=(
                    f"{game_id}:{task_input.round_id}:"
                    f"{joined.game_agent_id}:decide"
                ),
                input_hash=sha256_identifier(task_input),
                input=task_input,
            )
            await core.initialize()
            await core.create_task(
                task=task,
                config_snapshot=config_snapshot,
                config_hash=joined.config_hash,
                created_at=datetime.now(timezone.utc),
            )
            pydantic_worker = DurableHostedWorker(
                repository=worker_repository,
                providers=ProductionProviderBundle(
                    registry=CapabilityRegistry(),
                    adapters={},
                ),
                secret_reader=secrets.reader,
                worker_id=f"pydantic-worker-{suffix}",
                model_factory=_TestModelFactory(),  # type: ignore[arg-type]
            )
            claimed_tasks = await worker_repository.claim_tasks(
                f"pydantic-worker-{suffix}",
                limit=1,
                lease_seconds=600,
            )
            assert len(claimed_tasks) == 1
            await pydantic_worker._execute_task(claimed_tasks[0])

            pending = await core.pending_results(limit=10)
            assert len(pending) == 1
            assert pending[0].result.task_id == task.task_id
            assert pending[0].result.status == "succeeded"
            await core.apply_result(
                result_id=pending[0].result.result_id,
                server_clock=lambda: datetime.now(timezone.utc),
            )
            assert (
                await worker_repository.project_memory_patches(limit=100)
                == 1
            )

            learned = await admin.fetchrow(
                """
                SELECT
                    memory.memory_version,
                    memory.last_applied_task_id,
                    patch.status AS patch_status
                FROM hosted_agent_game_memory AS memory
                JOIN hosted_agent_memory_patches AS patch
                  ON patch.game_agent_id = memory.game_agent_id
                WHERE memory.game_agent_id = $1
                  AND patch.task_id = $2
                """,
                joined.game_agent_id,
                task.task_id,
            )
            assert learned is not None
            assert learned["memory_version"] == 1
            assert learned["last_applied_task_id"] == task.task_id
            assert learned["patch_status"] == "applied"

            default_deadline = datetime.now(timezone.utc) + timedelta(
                seconds=30
            )
            default_round_id = f"{game_id}:round:2"
            await admin.execute(
                """
                INSERT INTO rounds (
                    round_id,
                    game_id,
                    round_index,
                    phase,
                    deadline_at
                )
                VALUES ($1, $2, 2, 'decide', $3)
                """,
                default_round_id,
                game_id,
                default_deadline,
            )
            default_input = decide_input(
                deadline=default_deadline
            ).model_copy(
                update={
                    "game_id": game_id,
                    "round_id": default_round_id,
                    "round_index": 2,
                }
            )
            default_task = ArenaAgentTaskV1(
                task_id=f"task-default-{suffix}",
                kind="arena.decide",
                schema_version=AGENT_TASK_SCHEMA_VERSION_V1,
                game_id=game_id,
                round_id=default_round_id,
                game_agent_id=joined.game_agent_id,
                deadline_at=default_deadline,
                idempotency_key=(
                    f"{game_id}:{default_round_id}:"
                    f"{joined.game_agent_id}:decide"
                ),
                input_hash=sha256_identifier(default_input),
                input=default_input,
            )
            await core.create_task(
                task=default_task,
                config_snapshot=config_snapshot,
                config_hash=joined.config_hash,
                created_at=datetime.now(timezone.utc),
            )
            default_worker_id = f"default-worker-{suffix}"
            default_claim = await worker_repository.claim_tasks(
                default_worker_id,
                limit=1,
                lease_seconds=600,
            )
            assert len(default_claim) == 1
            never_submitted_digest = sha256_text_identifier(
                f"candidate-never-submitted-{suffix}"
            )
            assert await worker_repository.stage_memory_patch(
                default_worker_id,
                default_task.task_id,
                runtime_result_id_digest=never_submitted_digest,
                expected_memory_version=1,
                decision_summary={
                    "plan": "This candidate must not be learned.",
                    "factors": ["The authoritative deadline will win."],
                    "confidence_bps": 5000,
                },
                memory_patch={
                    "round_summary": "A staged candidate lost its deadline.",
                    "next_plan": "Do not learn from the default result.",
                    "observations": [],
                    "strategy_adjustments": [],
                    "risk_budget_bps": 4000,
                },
            )
            await admin.execute(
                """
                UPDATE arena_agent_tasks
                SET deadline_at = clock_timestamp() - INTERVAL '1 second'
                WHERE task_id = $1
                """,
                default_task.task_id,
            )
            finalized = await core.finalize_expired(
                server_clock=lambda: datetime.now(timezone.utc),
                limit=10,
            )
            assert any(
                item.result.task_id == default_task.task_id
                for item in finalized
            )
            assert (
                await worker_repository.project_memory_patches(limit=100)
                == 1
            )
            discarded = await admin.fetchrow(
                """
                SELECT
                    memory.memory_version,
                    memory.last_applied_task_id,
                    patch.status AS patch_status
                FROM hosted_agent_game_memory AS memory
                JOIN hosted_agent_memory_patches AS patch
                  ON patch.game_agent_id = memory.game_agent_id
                WHERE memory.game_agent_id = $1
                  AND patch.task_id = $2
                """,
                joined.game_agent_id,
                default_task.task_id,
            )
            assert discarded is not None
            assert discarded["memory_version"] == 1
            assert discarded["last_applied_task_id"] == task.task_id
            assert discarded["patch_status"] == "discarded"
        finally:
            await core.close()
            await participation.close()
            await worker_repository.close()
            await control.close()
            await admin.close()

    asyncio.run(scenario())
