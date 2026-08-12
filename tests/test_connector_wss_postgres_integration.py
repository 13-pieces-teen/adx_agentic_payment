from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from connector_gateway.models import CommandAction, RuntimeInventoryItem
from connector_gateway.persistent_service import PersistentConnectorGateway
from connector_gateway.postgres_repository import PostgresConnectorRepository


ADMIN_URL = os.getenv("ADX_TEST_POSTGRES_ADMIN_URL")
API_URL = os.getenv("ADX_TEST_POSTGRES_API_URL")

pytestmark = pytest.mark.skipif(
    not ADMIN_URL or not API_URL,
    reason="real PostgreSQL integration URLs are not configured",
)


class _Socket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed: list[tuple[int, str]] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int, reason: str) -> None:
        self.closed.append((code, reason))


def test_two_wss_instances_take_over_and_replay_with_real_postgres() -> None:
    async def scenario() -> None:
        import asyncpg

        suffix = uuid.uuid4().hex[:12]
        owner_id = f"connector-wss-owner-{suffix}"
        admin = await asyncpg.connect(ADMIN_URL)
        control = PersistentConnectorGateway(
            PostgresConnectorRepository(API_URL),
            instance_id=f"control-{suffix}",
        )
        first = PersistentConnectorGateway(
            PostgresConnectorRepository(API_URL),
            instance_id=f"wss-a-{suffix}",
        )
        second = PersistentConnectorGateway(
            PostgresConnectorRepository(API_URL),
            instance_id=f"wss-b-{suffix}",
        )
        device_id = ""
        try:
            await admin.execute(
                """
                INSERT INTO connector_users (
                    user_id, username, password_hash, temporary
                )
                VALUES ($1, $2, 'integration-only-hash', FALSE)
                """,
                owner_id,
                f"connector_wss_{suffix}",
            )
            await control.initialize()
            pairing = await control.create_pairing(None, "Postgres WSS laptop")
            await control.approve_pairing(pairing["user_code"], owner_id)
            credential = await control.exchange_pairing(pairing["device_code"])
            device_id = credential["device_id"]
            await control.update_inventory(
                device_id,
                [
                    RuntimeInventoryItem(
                        runtime_id="codex-default",
                        kind="codex",
                        display_name="Codex",
                        executable_path="codex",
                        capabilities=[],
                    )
                ],
            )
            binding = await control.create_binding(
                device_id,
                "codex-default",
                None,
                None,
            )
            await first.initialize()
            await second.initialize()
            first_socket = _Socket()
            first_generation = await first.connect_device(device_id, first_socket)
            await first.mark_transport_ready(device_id, first_generation)
            command = await control.queue_command(
                binding["binding_id"],
                CommandAction.RUNTIME_PROBE,
                {},
                f"postgres-takeover-{suffix}",
                300,
            )
            assert await first.route_shared_commands_once() == 1
            assert first_socket.sent[0]["message_id"] == command["command_id"]

            second_socket = _Socket()
            second_generation = await second.connect_device(
                device_id,
                second_socket,
            )
            await second.mark_transport_ready(device_id, second_generation)
            assert await second.route_shared_commands_once() == 1
            assert second_socket.sent[0]["message_id"] == command["command_id"]
            await second.acknowledge_command(
                device_id,
                {
                    "command_id": command["command_id"],
                    "status": "succeeded",
                    "result": {"available": True},
                },
                second_generation,
            )

            observed_device = next(
                item
                for item in await control.list_devices(owner_id)
                if item["device_id"] == device_id
            )
            assert observed_device["status"] == "online"

            observed = await control.list_commands(binding["binding_id"])
            assert next(
                item
                for item in observed
                if item["command_id"] == command["command_id"]
            )["status"] == "succeeded"
            audit = await control.list_audit(owner_id=owner_id)
            assert any(
                item["action"] == "command.acknowledged"
                and item["metadata"]["command_id"] == command["command_id"]
                for item in audit
            )
        finally:
            await second.close()
            await first.close()
            await control.close()
            await admin.execute(
                "DELETE FROM connector_pairings WHERE owner_id = $1",
                owner_id,
            )
            await admin.execute(
                "DELETE FROM connector_devices WHERE owner_id = $1",
                owner_id,
            )
            await admin.execute(
                "UPDATE connector_audit SET owner_id = NULL WHERE owner_id = $1",
                owner_id,
            )
            # Retain the isolated integration user because other Arena tables
            # may reference it through append-only evidence.
            await admin.close()

    asyncio.run(scenario())
