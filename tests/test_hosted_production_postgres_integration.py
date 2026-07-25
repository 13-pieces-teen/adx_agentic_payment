from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from arena_core import PostgresArenaParticipationRepository
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


ADMIN_URL = os.getenv("ADX_TEST_POSTGRES_ADMIN_URL")
API_URL = os.getenv("ADX_TEST_POSTGRES_API_URL")
WORKER_URL = os.getenv("ADX_TEST_POSTGRES_WORKER_URL")
pytestmark = pytest.mark.skipif(
    not (ADMIN_URL and API_URL and WORKER_URL),
    reason="real PostgreSQL integration URLs are not configured",
)


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


def test_create_validate_and_join_survive_process_boundaries() -> None:
    async def scenario() -> None:
        import asyncpg

        suffix = uuid.uuid4().hex[:12]
        owner_id = f"user-{suffix}"
        admin = await asyncpg.connect(ADMIN_URL)
        control = PostgresHostedAgentControlRepository(API_URL)
        worker_repository = PostgresHostedWorkerRepository(WORKER_URL)
        participation = PostgresArenaParticipationRepository(API_URL)
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
                INSERT INTO games (
                    game_id, status, action_timeout_ms, config_snapshot
                )
                VALUES (
                    $1, 'open', 30000,
                    '{"initial_cash_atomic":1000000,'
                    '"initial_inventory":{"ruby":1}}'::jsonb
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
        finally:
            await participation.close()
            await worker_repository.close()
            await control.close()
            await admin.close()

    asyncio.run(scenario())
