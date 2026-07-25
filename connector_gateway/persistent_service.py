"""Durable adapter around the Connector protocol/state machine."""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from arena_agent_contracts import AgentTaskResultV1

from .models import CommandAction, RuntimeInventoryItem
from .repository import ConnectorRepository
from .service import ConnectorError, ConnectorGateway


class AgentTaskResultSink(Protocol):
    async def submit(self, result: AgentTaskResultV1) -> Any: ...


class PersistentConnectorGateway(ConnectorGateway):
    """Persist every completed state transition to a Connector repository.

    The deployment intentionally runs one ASGI worker: sockets and send locks
    are process-local, while all protocol state survives restarts in PostgreSQL.
    """

    def __init__(
        self,
        repository: ConnectorRepository,
        verification_uri: Optional[str] = None,
        max_pending_pairings: int = 500,
        agent_task_result_sink: AgentTaskResultSink | None = None,
    ) -> None:
        super().__init__(
            verification_uri=verification_uri,
            max_pending_pairings=max_pending_pairings,
        )
        self.repository = repository
        self._agent_task_result_sink = agent_task_result_sink
        self._initialization_lock = asyncio.Lock()
        self._persistence_lock = asyncio.Lock()
        self._persisted_event_ids: set[str] = set()
        self._persisted_audit_ids: set[str] = set()
        self._initialized = False

    def bind_agent_task_result_sink(self, sink: AgentTaskResultSink) -> None:
        if self._initialized:
            raise RuntimeError("AgentTask Result Sink must be bound before initialization")
        self._agent_task_result_sink = sink

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return
            await self.repository.initialize()
            state = await self.repository.load_gateway_state()
            async with self._lock:
                self.pairings = {
                    str(record["user_code"]).upper(): record
                    for record in state.get("pairings", [])
                }
                self.pairings_by_device_code = {
                    str(record["device_code_hash"]): str(record["user_code"]).upper()
                    for record in self.pairings.values()
                    if record.get("device_code_hash")
                    and record.get("status") in {"pending", "approved"}
                }
                self.devices = {
                    str(record["device_id"]): record
                    for record in state.get("devices", [])
                }
                self.bindings = {
                    str(record["binding_id"]): record
                    for record in state.get("bindings", [])
                }
                self.commands = {
                    str(record["command_id"]): record
                    for record in state.get("commands", [])
                }
                self.agent_task_results = {
                    str(record["task_id"]): record
                    for record in state.get("agent_task_results", [])
                }
                self.events = list(state.get("events", []))
                self.audit = list(state.get("audit", []))
                self._persisted_event_ids = {
                    str(record["event_id"]) for record in self.events
                }
                self._persisted_audit_ids = {
                    str(record["audit_id"]) for record in self.audit
                }
                for device in self.devices.values():
                    device["status"] = (
                        "revoked" if device.get("revoked_at") else "offline"
                    )
                    device["_connection_generation"] = (
                        int(device.get("_connection_generation", 0)) + 1
                    )
                for command in self.commands.values():
                    if command.get("status") == "delivered":
                        command["status"] = "queued"
                        command["delivered_at"] = None
                    # Presence in the loaded snapshot proves that the command
                    # crossed the durable barrier before the prior shutdown.
                    command["_durable_ready"] = True
                self._remove_expired_pairings(datetime.now(timezone.utc))
                self._rebuild_event_watermarks()
            self._initialized = True
            await self._persist_current()

    async def close(self) -> None:
        await self.repository.close()

    async def _persist_current(self) -> None:
        async with self._persistence_lock:
            async with self._lock:
                new_events = [
                    event
                    for event in self.events
                    if str(event["event_id"]) not in self._persisted_event_ids
                ]
                new_audit = [
                    item
                    for item in self.audit
                    if str(item["audit_id"]) not in self._persisted_audit_ids
                ]
                state = {
                    "pairings": copy.deepcopy(list(self.pairings.values())),
                    "devices": copy.deepcopy(list(self.devices.values())),
                    "bindings": copy.deepcopy(list(self.bindings.values())),
                    "commands": copy.deepcopy(list(self.commands.values())),
                    "agent_task_results": copy.deepcopy(
                        list(self.agent_task_results.values())
                    ),
                    # Append-only streams are sent as deltas. Replaying every
                    # historical event on each 15-second heartbeat becomes
                    # quadratic and can exhaust the small beta host.
                    "events": copy.deepcopy(new_events),
                    "audit": copy.deepcopy(new_audit),
                }
            await self.repository.save_gateway_state(state)
            async with self._lock:
                self._persisted_event_ids.update(
                    str(event["event_id"]) for event in new_events
                )
                self._persisted_event_ids.intersection_update(
                    str(event["event_id"]) for event in self.events
                )
                self._persisted_audit_ids.update(
                    str(item["audit_id"]) for item in new_audit
                )
                self._persisted_audit_ids.intersection_update(
                    str(item["audit_id"]) for item in self.audit
                )

    def _rebuild_event_watermarks(self) -> None:
        self.event_ack_watermarks = {}
        self.event_pending_sequences = {}
        sequences_by_device: dict[str, set[int]] = {}
        for event in self.events:
            sequences_by_device.setdefault(str(event["device_id"]), set()).add(
                int(event["sequence"])
            )
        for device_id, device in self.devices.items():
            watermark = max(0, int(device.get("event_ack_watermark", 0)))
            pending = {
                int(value)
                for value in device.get("event_pending_sequences", [])
                if int(value) > watermark
            }
            pending.update(
                value
                for value in sequences_by_device.get(device_id, set())
                if value > watermark
            )
            while watermark + 1 in pending:
                pending.remove(watermark + 1)
                watermark += 1
            self.event_ack_watermarks[device_id] = watermark
            self.event_pending_sequences[device_id] = pending
            device["event_ack_watermark"] = watermark
            device["event_pending_sequences"] = sorted(pending)

    async def create_pairing(
        self, requested_owner_id: Optional[str], device_name: str
    ) -> dict[str, Any]:
        await self.initialize()
        result = await super().create_pairing(requested_owner_id, device_name)
        await self._persist_current()
        return result

    async def approve_pairing(self, user_code: str, owner_id: str) -> dict[str, Any]:
        await self.initialize()
        result = await super().approve_pairing(user_code, owner_id)
        await self._persist_current()
        return result

    async def exchange_pairing(self, device_code: str) -> dict[str, Any]:
        await self.initialize()
        result = await super().exchange_pairing(device_code)
        await self._persist_current()
        return result

    async def authenticate_device(self, device_id: str, token: str) -> dict[str, Any]:
        await self.initialize()
        return await super().authenticate_device(device_id, token)

    async def connect_device(self, device_id: str, websocket: Any) -> int:
        await self.initialize()
        result = await super().connect_device(device_id, websocket)
        await self._persist_current()
        return result

    async def disconnect_device(self, device_id: str, websocket: Any) -> None:
        await self.initialize()
        await super().disconnect_device(device_id, websocket)
        await self._persist_current()

    async def apply_hello(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        await self.initialize()
        result = await super().apply_hello(device_id, payload, expected_generation)
        await self._persist_current()
        return result

    async def heartbeat(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> None:
        await self.initialize()
        await super().heartbeat(device_id, payload, expected_generation)
        await self._persist_current()

    async def update_inventory(
        self,
        device_id: str,
        runtimes: list[RuntimeInventoryItem],
        host: Optional[dict[str, Any]] = None,
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        await self.initialize()
        result = await super().update_inventory(
            device_id, runtimes, host, expected_generation
        )
        await self._persist_current()
        return result

    async def list_devices(
        self, owner_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        await self.initialize()
        return await super().list_devices(owner_id)

    async def get_device(self, device_id: str) -> dict[str, Any]:
        await self.initialize()
        return await super().get_device(device_id)

    async def revoke_device(self, device_id: str, owner_id: str) -> dict[str, Any]:
        await self.initialize()
        result = await super().revoke_device(device_id, owner_id)
        await self._persist_current()
        return result

    async def create_binding(
        self,
        device_id: str,
        runtime_id: str,
        agent_id: Optional[str],
        display_name: Optional[str],
    ) -> dict[str, Any]:
        await self.initialize()
        result = await super().create_binding(
            device_id, runtime_id, agent_id, display_name
        )
        await self._persist_current()
        return result

    async def list_bindings(
        self, device_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        await self.initialize()
        return await super().list_bindings(device_id)

    async def queue_command(
        self,
        binding_id: str,
        action: CommandAction,
        payload: dict[str, Any],
        idempotency_key: Optional[str],
        expires_in_seconds: int,
    ) -> dict[str, Any]:
        await self.initialize()
        result = await super().queue_command(
            binding_id,
            action,
            payload,
            idempotency_key,
            expires_in_seconds,
        )
        await self._persist_current()
        return result

    async def _prepare_command_delivery(self, command_id: str) -> None:
        # The full queued command, its idempotency key and its audit record must
        # be committed before the active WebSocket can observe the command.
        await self._persist_current()

    async def deliver_pending(self, device_id: str) -> int:
        await self.initialize()
        result = await super().deliver_pending(device_id)
        await self._persist_current()
        return result

    async def observe_inbound_sequence(
        self,
        device_id: str,
        sequence: Optional[int],
        expected_generation: Optional[int] = None,
    ) -> None:
        await self.initialize()
        await super().observe_inbound_sequence(device_id, sequence, expected_generation)
        await self._persist_current()

    async def acknowledge_command(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        await self.initialize()
        result = await super().acknowledge_command(
            device_id, payload, expected_generation
        )
        await self._persist_current()
        return result

    async def append_runtime_event(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        await self.initialize()
        result = await super().append_runtime_event(
            device_id, payload, expected_generation
        )
        await self._persist_current()
        return result

    async def submit_agent_task_result(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        await self.initialize()
        result = await super().submit_agent_task_result(
            device_id,
            payload,
            expected_generation,
        )
        # Persist the immutable Gateway inbox before calling Arena. If the
        # Sink is unavailable, a process restart can still replay the exact
        # result while the Connector retains its own unacknowledged copy.
        await self._persist_current()
        if self._agent_task_result_sink is not None:
            # Re-submit exact Gateway replays to the Arena-owned idempotent
            # sink. This closes a crash window where Arena committed but the
            # Connector did not receive its transport acknowledgement.
            try:
                await self._agent_task_result_sink.submit(
                    AgentTaskResultV1.model_validate(payload.get("result"))
                )
            except Exception as exc:
                # The Connector must retry, but internal Arena or database
                # details must not cross the public WebSocket boundary.
                raise ConnectorError(503, "Arena Result Sink unavailable") from exc
        # The Connector receives its result acknowledgement only after both
        # the Gateway durable inbox and the configured Arena Sink succeed.
        return result

    async def list_events(
        self, binding_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        await self.initialize()
        return await super().list_events(binding_id, limit)

    async def list_commands(
        self, binding_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        await self.initialize()
        return await super().list_commands(binding_id, limit)

    async def list_audit(
        self, limit: int = 200, owner_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        await self.initialize()
        return await super().list_audit(limit, owner_id)
