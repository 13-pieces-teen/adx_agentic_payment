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
    AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
    AgentTaskResultV1,
    ArenaAgentTaskV1,
    BuyAction,
    PassAction,
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
from hosted_agent_runtime.attempts import AttemptCompletion, AttemptCreated
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
                "action": {"action": "buy", "good": "ruby"},
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
    def __init__(self) -> None:
        self.build_count = 0

    def build(self, **_: object) -> _BuiltTestModel:
        self.build_count += 1
        return _BuiltTestModel()


class _BuiltLearningTestModel:
    def __init__(self) -> None:
        self.model = TestModel(
            custom_output_args={
                "policyProfile": {
                    "riskBudgetBps": 5300,
                    "minExpectedEdgeBps": 1000,
                    "maxInventoryConcentrationBps": 7200,
                    "negotiationConcessionBps": 1100,
                    "explorationBps": 1300,
                },
                "lessonSummary": (
                    "Preserve a little more liquidity after this result."
                ),
                "adjustments": [
                    "Preserve slightly more cash before the final event.",
                    "Require a modestly clearer concentration edge.",
                ],
                "expectedEffect": (
                    "Reduce concentration without abandoning good trades."
                ),
                "confidenceBps": 7500,
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


class _LearningTestModelFactory:
    def build(self, **_: object) -> _BuiltLearningTestModel:
        return _BuiltLearningTestModel()


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

            pending = [
                item
                for item in await core.pending_results(limit=100)
                if item.result.task_id == task.task_id
            ]
            assert len(pending) == 1
            assert pending[0].result.task_id == task.task_id
            assert pending[0].result.status == "succeeded"
            await core.apply_result(
                result_id=pending[0].result.result_id,
                server_clock=lambda: datetime.now(timezone.utc),
            )

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
            next_context = await worker_repository.load_runtime_context(
                default_worker_id,
                default_task.task_id,
            )
            assert next_context["memoryVersion"] == 1

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

            async def create_recovery_task(
                label: str,
                round_index: int,
            ) -> ArenaAgentTaskV1:
                recovery_deadline = datetime.now(
                    timezone.utc
                ) + timedelta(seconds=30)
                recovery_round_id = (
                    f"{game_id}:runtime-recovery:{round_index}:{label}"
                )
                await admin.execute(
                    """
                    INSERT INTO rounds (
                        round_id,
                        game_id,
                        round_index,
                        phase,
                        deadline_at
                    )
                    VALUES ($1, $2, $3, 'decide', $4)
                    """,
                    recovery_round_id,
                    game_id,
                    round_index,
                    recovery_deadline,
                )
                recovery_input = decide_input(
                    deadline=recovery_deadline
                ).model_copy(
                    update={
                        "game_id": game_id,
                        "round_id": recovery_round_id,
                        "round_index": round_index,
                    }
                )
                recovery_task = ArenaAgentTaskV1(
                    task_id=f"task-{label}-{suffix}",
                    kind="arena.decide",
                    schema_version=AGENT_TASK_SCHEMA_VERSION_V1,
                    game_id=game_id,
                    round_id=recovery_round_id,
                    game_agent_id=joined.game_agent_id,
                    deadline_at=recovery_deadline,
                    idempotency_key=(
                        f"{game_id}:{recovery_round_id}:"
                        f"{joined.game_agent_id}:decide"
                    ),
                    input_hash=sha256_identifier(recovery_input),
                    input=recovery_input,
                )
                await core.create_task(
                    task=recovery_task,
                    config_snapshot=config_snapshot,
                    config_hash=joined.config_hash,
                    created_at=datetime.now(timezone.utc),
                )
                return recovery_task

            async def apply_only_pending_result(task_id: str) -> None:
                matching = [
                    item
                    for item in await core.pending_results(limit=100)
                    if item.result.task_id == task_id
                ]
                assert len(matching) == 1
                await core.apply_result(
                    result_id=matching[0].result.result_id,
                    server_clock=lambda: datetime.now(timezone.utc),
                )

            # Two Worker replicas must not receive the same queued task.
            race_task = await create_recovery_task("claim-race", 20)
            race_claims = await asyncio.gather(
                worker_repository.claim_tasks(
                    f"race-worker-a-{suffix}",
                    limit=1,
                    lease_seconds=600,
                ),
                worker_repository.claim_tasks(
                    f"race-worker-b-{suffix}",
                    limit=1,
                    lease_seconds=600,
                ),
            )
            assert sorted(len(claims) for claims in race_claims) == [0, 1]
            race_index = 0 if race_claims[0] else 1
            race_worker_id = (
                f"race-worker-{'a' if race_index == 0 else 'b'}-{suffix}"
            )
            race_factory = _TestModelFactory()
            race_worker = DurableHostedWorker(
                repository=worker_repository,
                providers=ProductionProviderBundle(
                    registry=CapabilityRegistry(),
                    adapters={},
                ),
                secret_reader=secrets.reader,
                worker_id=race_worker_id,
                model_factory=race_factory,  # type: ignore[arg-type]
            )
            await race_worker._execute_task(race_claims[race_index][0])
            assert race_factory.build_count == 1
            await apply_only_pending_result(race_task.task_id)

            # A crash after creating the Attempt but before request_sent is
            # safe to retry because no Provider side effect can have occurred.
            before_send_task = await create_recovery_task(
                "crash-before-send",
                21,
            )
            before_send_worker_a = f"before-send-a-{suffix}"
            before_send_claim = await worker_repository.claim_tasks(
                before_send_worker_a,
                limit=1,
                lease_seconds=600,
            )
            assert len(before_send_claim) == 1
            before_send_attempt_id = f"attempt-before-send-{suffix}"
            assert (
                await worker_repository.start_attempt(
                    before_send_worker_a,
                    AttemptCreated(
                        attempt_id=before_send_attempt_id,
                        task_id=before_send_task.task_id,
                        attempt_number=1,
                        provider_id=before_send_claim[0].provider,
                        model_id="deepseek-v4-flash",
                        thinking_enabled=False,
                        created_at=datetime.now(timezone.utc),
                    ),
                )
                == 1
            )
            await admin.execute(
                """
                UPDATE arena_agent_tasks
                SET lease_expires_at =
                    clock_timestamp() - INTERVAL '1 second'
                WHERE task_id = $1
                """,
                before_send_task.task_id,
            )
            before_send_worker_b = f"before-send-b-{suffix}"
            before_send_reclaim = await worker_repository.claim_tasks(
                before_send_worker_b,
                limit=1,
                lease_seconds=600,
            )
            assert len(before_send_reclaim) == 1
            assert before_send_reclaim[0].recovery_disposition == "execute"
            assert before_send_reclaim[0].first_attempt_number == 2
            interrupted_attempt = await admin.fetchrow(
                """
                SELECT status, error_class
                FROM arena_agent_task_attempts
                WHERE attempt_id = $1
                """,
                before_send_attempt_id,
            )
            assert interrupted_attempt is not None
            assert interrupted_attempt["status"] == "failed"
            assert (
                interrupted_attempt["error_class"]
                == "interrupted_before_send"
            )
            before_send_factory = _TestModelFactory()
            before_send_worker = DurableHostedWorker(
                repository=worker_repository,
                providers=ProductionProviderBundle(
                    registry=CapabilityRegistry(),
                    adapters={},
                ),
                secret_reader=secrets.reader,
                worker_id=before_send_worker_b,
                model_factory=before_send_factory,  # type: ignore[arg-type]
            )
            await before_send_worker._execute_task(before_send_reclaim[0])
            assert before_send_factory.build_count == 1
            await apply_only_pending_result(before_send_task.task_id)

            # Once request_sent is durable, a restarted Worker must not replay
            # the Provider call. It terminates the task with unknown outcome.
            after_send_task = await create_recovery_task(
                "crash-after-send",
                22,
            )
            after_send_worker_a = f"after-send-a-{suffix}"
            after_send_claim = await worker_repository.claim_tasks(
                after_send_worker_a,
                limit=1,
                lease_seconds=600,
            )
            assert len(after_send_claim) == 1
            after_send_attempt_id = f"attempt-after-send-{suffix}"
            assert (
                await worker_repository.start_attempt(
                    after_send_worker_a,
                    AttemptCreated(
                        attempt_id=after_send_attempt_id,
                        task_id=after_send_task.task_id,
                        attempt_number=1,
                        provider_id=after_send_claim[0].provider,
                        model_id="deepseek-v4-flash",
                        thinking_enabled=False,
                        created_at=datetime.now(timezone.utc),
                    ),
                )
                == 1
            )
            assert await worker_repository.mark_attempt_sent(
                after_send_worker_a,
                after_send_attempt_id,
            )
            await admin.execute(
                """
                UPDATE arena_agent_tasks
                SET lease_expires_at =
                    clock_timestamp() - INTERVAL '1 second'
                WHERE task_id = $1
                """,
                after_send_task.task_id,
            )
            after_send_worker_b = f"after-send-b-{suffix}"
            after_send_reclaim = await worker_repository.claim_tasks(
                after_send_worker_b,
                limit=1,
                lease_seconds=600,
            )
            assert len(after_send_reclaim) == 1
            assert (
                after_send_reclaim[0].recovery_disposition
                == "terminal_unknown"
            )
            after_send_factory = _TestModelFactory()
            after_send_worker = DurableHostedWorker(
                repository=worker_repository,
                providers=ProductionProviderBundle(
                    registry=CapabilityRegistry(),
                    adapters={},
                ),
                secret_reader=secrets.reader,
                worker_id=after_send_worker_b,
                model_factory=after_send_factory,  # type: ignore[arg-type]
            )
            await after_send_worker._execute_task(after_send_reclaim[0])
            assert after_send_factory.build_count == 0
            after_send_state = await admin.fetchrow(
                """
                SELECT
                    task.status AS task_status,
                    task.terminal_reason,
                    attempt.status AS attempt_status,
                    attempt.error_class
                FROM arena_agent_tasks AS task
                JOIN arena_agent_task_attempts AS attempt
                  ON attempt.task_id = task.task_id
                WHERE task.task_id = $1
                """,
                after_send_task.task_id,
            )
            assert after_send_state is not None
            assert after_send_state["task_status"] == "defaulted"
            assert (
                after_send_state["terminal_reason"]
                == "request_outcome_unknown"
            )
            assert after_send_state["attempt_status"] == "unknown"
            assert (
                after_send_state["error_class"]
                == "request_outcome_unknown"
            )
            await apply_only_pending_result(after_send_task.task_id)

            # Concurrent identical submissions are serialized into exactly one
            # accepted Result and one durable duplicate receipt.
            cas_task = await create_recovery_task("result-cas", 23)
            cas_worker_id = f"result-cas-worker-{suffix}"
            cas_claim = await worker_repository.claim_tasks(
                cas_worker_id,
                limit=1,
                lease_seconds=600,
            )
            assert len(cas_claim) == 1
            cas_attempt_id = f"attempt-result-cas-{suffix}"
            assert (
                await worker_repository.start_attempt(
                    cas_worker_id,
                    AttemptCreated(
                        attempt_id=cas_attempt_id,
                        task_id=cas_task.task_id,
                        attempt_number=1,
                        provider_id=cas_claim[0].provider,
                        model_id="deepseek-v4-flash",
                        thinking_enabled=False,
                        created_at=datetime.now(timezone.utc),
                    ),
                )
                == 1
            )
            assert await worker_repository.mark_attempt_sent(
                cas_worker_id,
                cas_attempt_id,
            )
            assert await worker_repository.finish_attempt(
                cas_worker_id,
                AttemptCompletion(
                    attempt_id=cas_attempt_id,
                    status="succeeded",
                    finished_at=datetime.now(timezone.utc),
                    latency_ms=1,
                    usage=ProviderUsage(
                        input_tokens=1,
                        output_tokens=1,
                        cached_input_tokens=0,
                        reasoning_tokens=0,
                        complete=True,
                    ),
                    actual_model="deepseek-v4-flash",
                ),
            )
            cas_result = AgentTaskResultV1(
                result_id=f"runtime-result-cas-{suffix}",
                task_id=cas_task.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="succeeded",
                action=PassAction(action="pass"),
            )
            cas_dispositions = await asyncio.gather(
                worker_repository.submit_result(
                    cas_worker_id,
                    cas_result,
                    message_replaced=False,
                    policy_version="arena.public-output.v1",
                    error_class=None,
                ),
                worker_repository.submit_result(
                    cas_worker_id,
                    cas_result,
                    message_replaced=False,
                    policy_version="arena.public-output.v1",
                    error_class=None,
                ),
            )
            assert sorted(cas_dispositions) == ["accepted", "duplicate"]
            conflicting_result = cas_result.model_copy(
                update={"status": "failed", "action": None}
            )
            assert (
                await worker_repository.submit_result(
                    cas_worker_id,
                    conflicting_result,
                    message_replaced=False,
                    policy_version=None,
                    error_class="runtime_failed",
                )
                == "conflict"
            )
            cas_state = await admin.fetchrow(
                """
                SELECT
                    count(DISTINCT result.result_id) AS result_count,
                    count(*) FILTER (
                        WHERE event_type = 'duplicate_result_ignored'
                    ) AS duplicate_count,
                    count(*) FILTER (
                        WHERE event_type = 'result_conflict'
                    ) AS conflict_count
                FROM arena_agent_task_results AS result
                CROSS JOIN arena_agent_task_events AS event
                WHERE result.task_id = $1
                  AND event.task_id = $1
                """,
                cas_task.task_id,
            )
            assert cas_state is not None
            assert cas_state["result_count"] == 1
            assert cas_state["duplicate_count"] == 1
            assert cas_state["conflict_count"] == 1
            await apply_only_pending_result(cas_task.task_id)

            # A structurally valid Runtime success can still be rejected by
            # Arena's semantic policy; the candidate never mutates the pool.
            rejected_task = await create_recovery_task(
                "semantic-rejection",
                25,
            )
            rejected_worker_id = f"rejected-worker-{suffix}"
            rejected_claim = await worker_repository.claim_tasks(
                rejected_worker_id,
                limit=1,
                lease_seconds=600,
            )
            assert len(rejected_claim) == 1
            rejected_context = await worker_repository.load_runtime_context(
                rejected_worker_id,
                rejected_task.task_id,
            )
            rejected_memory_version = int(
                rejected_context["memoryVersion"]
            )
            rejected_attempt_id = f"attempt-rejected-{suffix}"
            assert (
                await worker_repository.start_attempt(
                    rejected_worker_id,
                    AttemptCreated(
                        attempt_id=rejected_attempt_id,
                        task_id=rejected_task.task_id,
                        attempt_number=1,
                        provider_id=rejected_claim[0].provider,
                        model_id="deepseek-v4-flash",
                        thinking_enabled=False,
                        created_at=datetime.now(timezone.utc),
                    ),
                )
                == 1
            )
            assert await worker_repository.mark_attempt_sent(
                rejected_worker_id,
                rejected_attempt_id,
            )
            assert await worker_repository.finish_attempt(
                rejected_worker_id,
                AttemptCompletion(
                    attempt_id=rejected_attempt_id,
                    status="succeeded",
                    finished_at=datetime.now(timezone.utc),
                    latency_ms=1,
                    usage=ProviderUsage(
                        input_tokens=1,
                        output_tokens=1,
                        cached_input_tokens=0,
                        reasoning_tokens=0,
                        complete=True,
                    ),
                    actual_model="deepseek-v4-flash",
                ),
            )
            rejected_result = AgentTaskResultV1(
                result_id=f"runtime-result-rejected-{suffix}",
                task_id=rejected_task.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="succeeded",
                action=BuyAction(
                    action="buy",
                    good="not-in-frozen-game",
                ),
            )
            assert await worker_repository.stage_memory_patch(
                rejected_worker_id,
                rejected_task.task_id,
                runtime_result_id_digest=sha256_text_identifier(
                    rejected_result.result_id
                ),
                expected_memory_version=rejected_memory_version,
                decision_summary={
                    "plan": "Try a candidate outside the frozen goods.",
                    "factors": ["This must be rejected by Arena."],
                    "confidence_bps": 5000,
                },
                memory_patch={
                    "round_summary": "Submitted an invalid candidate.",
                    "next_plan": "This patch must never be learned.",
                    "observations": [],
                    "strategy_adjustments": [],
                    "risk_budget_bps": 5000,
                },
            )
            assert (
                await worker_repository.submit_result(
                    rejected_worker_id,
                    rejected_result,
                    message_replaced=False,
                    policy_version="arena.public-output.v1",
                    error_class=None,
                )
                == "accepted"
            )
            await apply_only_pending_result(rejected_task.task_id)
            rejected_state = await admin.fetchrow(
                """
                SELECT
                    result.apply_status,
                    result.arena_applied_at,
                    result.error_class,
                    application.application_outcome,
                    application.applied_action
                FROM arena_agent_task_results AS result
                JOIN arena_applied_agent_actions AS application
                  ON application.result_id = result.result_id
                WHERE result.task_id = $1
                """,
                rejected_task.task_id,
            )
            assert rejected_state is not None
            assert rejected_state["apply_status"] == "applied"
            assert rejected_state["arena_applied_at"] is not None
            assert rejected_state["error_class"] == "good_not_allowed"
            assert (
                rejected_state["application_outcome"]
                == "default_pass"
            )
            rejected_applied_action = rejected_state["applied_action"]
            assert (
                json.loads(rejected_applied_action)
                if isinstance(rejected_applied_action, str)
                else dict(rejected_applied_action)
            ) == {"action": "pass"}
            assert (
                await admin.fetchval(
                    """
                    SELECT count(*)
                    FROM arena402.pool_entries
                    WHERE source_result_id = (
                        SELECT result_id
                        FROM arena_agent_task_results
                        WHERE task_id = $1
                    )
                    """,
                    rejected_task.task_id,
                )
                == 0
            )
            assert (
                await worker_repository.project_memory_patches(limit=100)
                == 1
            )
            rejected_memory = await admin.fetchrow(
                """
                SELECT memory.memory_version, patch.status AS patch_status
                FROM hosted_agent_game_memory AS memory
                JOIN hosted_agent_memory_patches AS patch
                  ON patch.game_agent_id = memory.game_agent_id
                WHERE patch.task_id = $1
                """,
                rejected_task.task_id,
            )
            assert rejected_memory is not None
            assert (
                rejected_memory["memory_version"]
                == rejected_memory_version
            )
            assert rejected_memory["patch_status"] == "discarded"

            # A Result arriving after the independent Finalizer wins is always
            # recorded as late and cannot replace the authoritative default.
            late_task = await create_recovery_task("late-result", 24)
            late_worker_id = f"late-worker-{suffix}"
            late_claim = await worker_repository.claim_tasks(
                late_worker_id,
                limit=1,
                lease_seconds=600,
            )
            assert len(late_claim) == 1
            await admin.execute(
                """
                UPDATE arena_agent_tasks
                SET deadline_at = clock_timestamp() - INTERVAL '1 second'
                WHERE task_id = $1
                """,
                late_task.task_id,
            )
            finalized_late = await core.finalize_expired(
                server_clock=lambda: datetime.now(timezone.utc),
                limit=100,
            )
            assert any(
                item.result.task_id == late_task.task_id
                for item in finalized_late
            )
            late_result = AgentTaskResultV1(
                result_id=f"runtime-result-late-{suffix}",
                task_id=late_task.task_id,
                schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
                status="succeeded",
                action=PassAction(action="pass"),
            )
            assert (
                await worker_repository.submit_result(
                    late_worker_id,
                    late_result,
                    message_replaced=False,
                    policy_version="arena.public-output.v1",
                    error_class=None,
                )
                == "late"
            )
            late_state = await admin.fetchrow(
                """
                SELECT
                    count(DISTINCT result.result_id) AS result_count,
                    count(*) FILTER (
                        WHERE event_type = 'late_result_ignored'
                    ) AS late_count
                FROM arena_agent_task_results AS result
                CROSS JOIN arena_agent_task_events AS event
                WHERE result.task_id = $1
                  AND event.task_id = $1
                """,
                late_task.task_id,
            )
            assert late_state is not None
            assert late_state["result_count"] == 1
            assert late_state["late_count"] == 1
            await apply_only_pending_result(late_task.task_id)

            opponent_id = f"opponent-{suffix}"
            await admin.execute(
                """
                INSERT INTO arena402.game_participants (
                    game_participant_id,
                    game_id,
                    user_id,
                    agent_id,
                    runtime_binding_id,
                    runtime_kind,
                    status,
                    portfolio_locked_at,
                    completed_at,
                    readiness,
                    ready_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, 'rule', 'completed',
                    clock_timestamp(), clock_timestamp(), 'ready',
                    clock_timestamp()
                )
                """,
                opponent_id,
                game_id,
                f"opponent-user-{suffix}",
                f"opponent-agent-{suffix}",
                f"opponent-binding-{suffix}",
            )
            await admin.execute(
                """
                INSERT INTO arena402.rounds (
                    round_id, game_id, round_index, phase,
                    completed_at
                )
                VALUES ($1, $2, 1, 'completed', clock_timestamp())
                """,
                round_id,
                game_id,
            )
            buyer_entry_id = f"learning-buyer-entry-{suffix}"
            seller_entry_id = f"learning-seller-entry-{suffix}"
            await admin.executemany(
                """
                INSERT INTO arena402.pool_entries (
                    pool_entry_id, game_id, round_id,
                    game_participant_id, source_result_id,
                    side, good_id, status, quantity,
                    limit_price_atomic
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, 'grain',
                    'paired', 1, 1000000
                )
                """,
                [
                    (
                        buyer_entry_id,
                        game_id,
                        round_id,
                        joined.game_agent_id,
                        f"learning-buyer-result-{suffix}",
                        "buy",
                    ),
                    (
                        seller_entry_id,
                        game_id,
                        round_id,
                        opponent_id,
                        f"learning-seller-result-{suffix}",
                        "sell",
                    ),
                ],
            )
            await admin.execute(
                """
                INSERT INTO arena402.pairings (
                    pairing_id, game_id, round_id, good_id,
                    buyer_entry_id, seller_entry_id,
                    buyer_participant_id, seller_participant_id,
                    pairing_sequence, status, completed_at, quantity,
                    buyer_limit_price_atomic,
                    seller_limit_price_atomic
                )
                VALUES (
                    $1, $2, $3, 'grain', $4, $5, $6, $7,
                    1, 'settled', clock_timestamp(), 1,
                    1000000, 1000000
                )
                """,
                f"learning-pairing-{suffix}",
                game_id,
                round_id,
                buyer_entry_id,
                seller_entry_id,
                joined.game_agent_id,
                opponent_id,
            )
            await admin.execute(
                """
                INSERT INTO arena402.final_settlement_prices (
                    game_id,
                    good_id,
                    price_atomic,
                    source_round_index
                )
                SELECT game_id, good_id, initial_price_atomic, 1
                FROM arena402.game_goods
                WHERE game_id = $1
                """,
                game_id,
            )
            await admin.executemany(
                """
                INSERT INTO arena402.rankings (
                    game_id,
                    game_participant_id,
                    rank,
                    net_worth_atomic,
                    tier
                )
                VALUES ($1, $2, $3, $4, $5)
                """,
                [
                    (
                        game_id,
                        joined.game_agent_id,
                        1,
                        21_000_000,
                        "公爵",
                    ),
                    (
                        game_id,
                        opponent_id,
                        2,
                        19_000_000,
                        "流浪商贩",
                    ),
                ],
            )
            await admin.execute(
                """
                UPDATE arena402.game_participants
                SET status = 'completed',
                    completed_at = COALESCE(
                        completed_at,
                        clock_timestamp()
                    )
                WHERE game_participant_id = $1
                """,
                joined.game_agent_id,
            )
            await admin.execute(
                """
                UPDATE arena402.games
                SET phase = 'completed',
                    completed_at = clock_timestamp()
                WHERE game_id = $1
                """,
                game_id,
            )
            await admin.execute(
                """
                UPDATE game_agents
                SET status = 'completed',
                    completed_at = clock_timestamp()
                WHERE game_agent_id = $1
                """,
                joined.game_agent_id,
            )
            await admin.execute(
                """
                UPDATE games
                SET status = 'completed',
                    completed_at = clock_timestamp()
                WHERE game_id = $1
                """,
                game_id,
            )

            base_revision_id = str(
                frozen["hosted_strategy_revision_id"]
            )
            crashed_learning_worker_id = (
                f"learning-worker-crashed-{suffix}"
            )
            crashed_learning_jobs = (
                await worker_repository.claim_learning_jobs(
                    crashed_learning_worker_id,
                    limit=1,
                    lease_seconds=30,
                )
            )
            assert len(crashed_learning_jobs) == 1
            assert crashed_learning_jobs[0].attempt_count == 1
            await admin.execute(
                """
                UPDATE hosted_agent_learning_jobs
                SET lease_expires_at =
                    clock_timestamp() - INTERVAL '1 second'
                WHERE learning_job_id = $1
                """,
                crashed_learning_jobs[0].learning_job_id,
            )
            learning_worker = DurableHostedWorker(
                repository=worker_repository,
                providers=ProductionProviderBundle(
                    registry=CapabilityRegistry(),
                    adapters={},
                ),
                secret_reader=secrets.reader,
                worker_id=f"learning-worker-{suffix}",
                model_factory=_LearningTestModelFactory(),  # type: ignore[arg-type]
            )
            # The restarted Worker reclaims and completes the expired durable
            # learning job without creating a second revision.
            assert await learning_worker.run_once() == 1

            learning_state = await admin.fetchrow(
                """
                SELECT
                    job.status AS job_status,
                    job.attempt_count,
                    job.candidate_strategy_revision_id,
                    active.strategy_revision_id AS active_revision_id,
                    active.source AS active_source,
                    active.parent_strategy_revision_id,
                    active.policy_profile,
                    frozen.hosted_strategy_revision_id
                        AS frozen_revision_id,
                    evaluation.outcome_score_bps
                FROM hosted_agent_learning_jobs AS job
                JOIN hosted_agent_strategy_revisions AS active
                  ON active.agent_id = job.agent_id
                 AND active.status = 'active'
                JOIN game_agents AS frozen
                  ON frozen.game_agent_id = job.game_agent_id
                JOIN hosted_agent_strategy_evaluations AS evaluation
                  ON evaluation.learning_job_id = job.learning_job_id
                WHERE job.game_agent_id = $1
                """,
                joined.game_agent_id,
            )
            assert learning_state is not None
            assert learning_state["job_status"] == "activated"
            assert learning_state["attempt_count"] == 2
            learned_revision_id = str(
                learning_state["active_revision_id"]
            )
            assert learned_revision_id != base_revision_id
            assert learning_state["active_source"] == "learned"
            assert (
                learning_state["parent_strategy_revision_id"]
                == base_revision_id
            )
            assert (
                learning_state["frozen_revision_id"]
                == base_revision_id
            )
            assert learning_state["outcome_score_bps"] == 500

            next_game_id = f"game-next-{suffix}"
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
                    $1, 1, 30000, 2, 2,
                    '{"initial_cash_atomic":1000000,'
                    '"initial_inventory":{"grain":1,"iron":1,'
                    '"warhorse":1,"gems":1},'
                    '"marketProtocol":"fcfs.v1"}'::jsonb,
                    'integration-next-event-seed',
                    $2,
                    'fcfs.v1'
                )
                """,
                next_game_id,
                sha256_text_identifier(
                    f"integration-next-event-seed:{next_game_id}"
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
                next_game_id,
            )
            await admin.execute(
                """
                INSERT INTO games (
                    game_id,
                    status,
                    action_timeout_ms,
                    config_snapshot
                )
                VALUES (
                    $1, 'open', 30000,
                    '{"initial_cash_atomic":1000000,'
                    '"initial_inventory":{"grain":1,"iron":1,'
                    '"warhorse":1,"gems":1}}'::jsonb
                )
                """,
                next_game_id,
            )
            next_joined = await participation.join(
                owner_user_id=owner_id,
                game_id=next_game_id,
                agent_id=created.agent_id,
                key_digest=sha256_text_identifier(
                    f"join-next-{suffix}"
                ),
                request_digest=sha256_identifier(
                    {
                        "agentId": created.agent_id,
                        "gameId": next_game_id,
                    }
                ),
            )
            next_frozen = await admin.fetchrow(
                """
                SELECT
                    hosted_strategy_revision_id,
                    config_snapshot ->> 'strategy_revision_id'
                        AS snapshot_revision_id,
                    config_snapshot ->> 'strategy_instructions'
                        AS snapshot_instructions
                FROM game_agents
                WHERE game_agent_id = $1
                """,
                next_joined.game_agent_id,
            )
            assert next_frozen is not None
            assert (
                next_frozen["hosted_strategy_revision_id"]
                == learned_revision_id
            )
            assert (
                next_frozen["snapshot_revision_id"]
                == learned_revision_id
            )
            assert "Learned bounded policy" in str(
                next_frozen["snapshot_instructions"]
            )

            await admin.execute(
                """
                UPDATE game_agents
                SET status = 'completed',
                    completed_at = clock_timestamp()
                WHERE game_agent_id = $1
                """,
                next_joined.game_agent_id,
            )
            await admin.execute(
                """
                UPDATE games
                SET status = 'completed',
                    completed_at = clock_timestamp()
                WHERE game_id = $1
                """,
                next_game_id,
            )
            rollback_jobs = await worker_repository.claim_learning_jobs(
                f"rollback-worker-{suffix}",
                limit=1,
                lease_seconds=600,
            )
            assert len(rollback_jobs) == 1
            rollback_result = await worker_repository.complete_learning_job(
                f"rollback-worker-{suffix}",
                rollback_jobs[0],
                evidence_hash=sha256_identifier(
                    {"gameId": next_game_id, "outcome": "severe-regression"}
                ),
                outcome_score_bps=-5000,
                source_config_hash=sha256_identifier(
                    {"learningJobId": rollback_jobs[0].learning_job_id}
                ),
                policy_profile={
                    "riskBudgetBps": 5200,
                    "minExpectedEdgeBps": 1000,
                    "maxInventoryConcentrationBps": 7200,
                    "negotiationConcessionBps": 1100,
                    "explorationBps": 1200,
                },
                instructions="A valid bounded rollback candidate.",
                proposal={
                    "policyProfile": {
                        "riskBudgetBps": 5200,
                        "minExpectedEdgeBps": 1000,
                        "maxInventoryConcentrationBps": 7200,
                        "negotiationConcessionBps": 1100,
                        "explorationBps": 1200,
                    },
                    "lessonSummary": "Regression evidence.",
                },
                gate_summary={
                    "schemaVersion": "arena.hosted-learning-gate.v1",
                    "outcomeScoreBps": -5000,
                    "checks": {"allPassed": True},
                },
                gate_passed=True,
                gate_reason="passed",
            )
            assert rollback_result["disposition"] == "rolled_back"
            assert (
                rollback_result["strategyRevisionId"]
                == base_revision_id
            )
            rollback_state = await admin.fetchrow(
                """
                SELECT
                    job.status AS job_status,
                    active.strategy_revision_id AS active_revision_id,
                    regressed.status AS regressed_revision_status,
                    frozen.hosted_strategy_revision_id
                        AS frozen_revision_id
                FROM hosted_agent_learning_jobs AS job
                JOIN hosted_agent_strategy_revisions AS active
                  ON active.agent_id = job.agent_id
                 AND active.status = 'active'
                JOIN hosted_agent_strategy_revisions AS regressed
                  ON regressed.strategy_revision_id =
                     job.base_strategy_revision_id
                JOIN game_agents AS frozen
                  ON frozen.game_agent_id = job.game_agent_id
                WHERE job.game_agent_id = $1
                """,
                next_joined.game_agent_id,
            )
            assert rollback_state is not None
            assert rollback_state["job_status"] == "rolled_back"
            assert (
                rollback_state["active_revision_id"]
                == base_revision_id
            )
            assert (
                rollback_state["regressed_revision_status"]
                == "rejected"
            )
            assert (
                rollback_state["frozen_revision_id"]
                == learned_revision_id
            )
        finally:
            await core.close()
            await participation.close()
            await worker_repository.close()
            await control.close()
            await admin.close()

    asyncio.run(scenario())
