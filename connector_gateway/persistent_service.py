"""Durable adapter around the Connector protocol/state machine."""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from arena_agent_contracts import AgentTaskResultV1

from .models import CommandAction, RuntimeInventoryItem
from .repository import ConnectionFenceError, ConnectorRepository
from .service import (
    ConnectionReplacedError,
    ConnectorError,
    ConnectorGateway,
    iso,
    utc_now,
)


class AgentTaskResultSink(Protocol):
    async def submit(self, result: AgentTaskResultV1) -> Any: ...


class PersistentConnectorGateway(ConnectorGateway):
    """Persist every completed state transition to a Connector repository.

    Device connection ownership and WSS mutations are fenced in the shared
    repository. Sockets and sender locks remain process-local by design; each
    Device has exactly one shared owner while the REST control plane stays a
    separate single writer.
    """

    def __init__(
        self,
        repository: ConnectorRepository,
        verification_uri: Optional[str] = None,
        max_pending_pairings: int = 500,
        agent_task_result_sink: AgentTaskResultSink | None = None,
        instance_id: str | None = None,
        connection_lease_seconds: int = 45,
    ) -> None:
        super().__init__(
            verification_uri=verification_uri,
            max_pending_pairings=max_pending_pairings,
        )
        self.repository = repository
        self.instance_id = instance_id or f"gateway-{uuid.uuid4().hex}"
        if not self.instance_id or len(self.instance_id) > 128:
            raise ValueError("Gateway instance_id must be 1-128 characters")
        if not 15 <= connection_lease_seconds <= 300:
            raise ValueError("Connection lease must be between 15 and 300 seconds")
        self.connection_lease_seconds = connection_lease_seconds
        self._connection_lease_tokens: dict[Any, int] = {}
        self._shared_command_route_lock = asyncio.Lock()
        self._agent_task_result_sink = agent_task_result_sink
        self._initialization_lock = asyncio.Lock()
        self._persistence_lock = asyncio.Lock()
        self._persisted_event_ids: set[str] = set()
        self._persisted_audit_ids: set[str] = set()
        self._initialized = False
        self._draining = False

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
            active_lease_device_ids = {
                str(record["device_id"])
                for record in state.get("devices", [])
                if await self.repository.has_active_device_connection(
                    str(record["device_id"])
                )
            }
            recovered_device_ids: set[str] = set()
            recovered_command_ids: set[str] = set()
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
                for device_id, device in self.devices.items():
                    if device_id in active_lease_device_ids:
                        continue
                    device["status"] = (
                        "revoked" if device.get("revoked_at") else "offline"
                    )
                    device["_connection_generation"] = (
                        int(device.get("_connection_generation", 0)) + 1
                    )
                    recovered_device_ids.add(device_id)
                for command in self.commands.values():
                    if (
                        command.get("status") == "delivered"
                        and str(command["device_id"]) not in active_lease_device_ids
                    ):
                        command["status"] = "queued"
                        command["delivered_at"] = None
                        recovered_command_ids.add(str(command["command_id"]))
                    # Presence in the loaded snapshot proves that the command
                    # crossed the durable barrier before the prior shutdown.
                    command["_durable_ready"] = True
                self._remove_expired_pairings(datetime.now(timezone.utc))
                self._rebuild_event_watermarks()
            self._initialized = True
            # Do not rewrite live owners from a standby process. Startup
            # recovery mutates only Devices without an unexpired shared lease.
            await self._persist_current(
                replace_pairings=True,
                device_ids=recovered_device_ids,
                command_ids=recovered_command_ids,
            )

    async def close(self) -> None:
        await self.begin_drain()
        # Locally replaced sockets can retain only already-fenced tokens until
        # their handlers exit; no current lease remains for them to release.
        self._connection_lease_tokens.clear()
        await self.repository.close()

    async def begin_drain(self) -> None:
        """Stop accepting WSS ownership and ask Connectors to reconnect."""

        self._draining = True
        active_connections = list(self.connections.items())
        for device_id, websocket in active_connections:
            try:
                await websocket.close(
                    code=1012,
                    reason="Gateway worker restarting",
                )
            except Exception:
                pass
            await self.disconnect_device(device_id, websocket)

    async def _persist_current(
        self,
        *,
        full: bool | None = None,
        pairing_ids: set[str] | None = None,
        device_ids: set[str] | None = None,
        runtime_device_ids: set[str] | None = None,
        binding_ids: set[str] | None = None,
        command_ids: set[str] | None = None,
        result_task_ids: set[str] | None = None,
        replace_pairings: bool = False,
        connection_fence: tuple[str, int] | None = None,
    ) -> None:
        """Persist only the entities touched by one completed transition.

        Events and audit records are append-only deltas.  Mutable records use
        idempotent upserts in PostgreSQL; limiting those upserts is important
        because heartbeats are frequent and the complete Connector snapshot
        grows with every enrolled device, binding and command.
        """
        if full is None:
            # Preserve the private helper's historical "snapshot now"
            # behavior for maintenance callers. Production transitions always
            # pass an explicit entity scope below.
            full = not any(
                (
                    pairing_ids,
                    device_ids,
                    runtime_device_ids,
                    binding_ids,
                    command_ids,
                    result_task_ids,
                    replace_pairings,
                )
            )
        async with self._persistence_lock:
            async with self._lock:
                selected_pairings = (
                    list(self.pairings.values())
                    if full or replace_pairings
                    else [
                        item
                        for item in self.pairings.values()
                        if str(item["pairing_id"]) in (pairing_ids or set())
                    ]
                )
                selected_devices = (
                    list(self.devices.values())
                    if full
                    else [
                        self.devices[item_id]
                        for item_id in device_ids or set()
                        if item_id in self.devices
                    ]
                )
                selected_bindings = (
                    list(self.bindings.values())
                    if full
                    else [
                        self.bindings[item_id]
                        for item_id in binding_ids or set()
                        if item_id in self.bindings
                    ]
                )
                selected_commands = (
                    list(self.commands.values())
                    if full
                    else [
                        self.commands[item_id]
                        for item_id in command_ids or set()
                        if item_id in self.commands
                    ]
                )
                selected_results = (
                    list(self.agent_task_results.values())
                    if full
                    else [
                        self.agent_task_results[item_id]
                        for item_id in result_task_ids or set()
                        if item_id in self.agent_task_results
                    ]
                )
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
                    "_incremental": not full,
                    "_replace_collections": (
                        ["pairings"] if replace_pairings and not full else []
                    ),
                    "_replace_runtime_device_ids": (
                        [str(item["device_id"]) for item in selected_devices]
                        if full
                        else sorted(runtime_device_ids or set())
                    ),
                    "pairings": copy.deepcopy(selected_pairings),
                    "devices": copy.deepcopy(selected_devices),
                    "bindings": copy.deepcopy(selected_bindings),
                    "commands": copy.deepcopy(selected_commands),
                    "agent_task_results": copy.deepcopy(selected_results),
                    # Append-only streams are sent as deltas. Replaying every
                    # historical event on each 15-second heartbeat becomes
                    # quadratic and can exhaust the small beta host.
                    "events": copy.deepcopy(new_events),
                    "audit": copy.deepcopy(new_audit),
                }
                if connection_fence is not None:
                    state["_connection_fence"] = {
                        "device_id": connection_fence[0],
                        "instance_id": self.instance_id,
                        "fencing_token": connection_fence[1],
                    }
            try:
                await self.repository.save_gateway_state(state)
            except ConnectionFenceError as exc:
                raise ConnectionReplacedError(
                    "WebSocket was replaced by a connection on another Gateway instance",
                ) from exc
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
        # Creating a pairing also removes expired pairings, so replace this
        # bounded collection while keeping every other entity incremental.
        await self._persist_current(replace_pairings=True)
        return result

    async def approve_pairing(self, user_code: str, owner_id: str) -> dict[str, Any]:
        await self.initialize()
        result = await super().approve_pairing(user_code, owner_id)
        await self._persist_current(pairing_ids={str(result["pairing_id"])})
        return result

    async def exchange_pairing(self, device_code: str) -> dict[str, Any]:
        await self.initialize()
        result = await super().exchange_pairing(device_code)
        await self._persist_current(
            device_ids={str(result["device_id"])},
            runtime_device_ids={str(result["device_id"])},
            replace_pairings=True,
        )
        return result

    async def authenticate_device(self, device_id: str, token: str) -> dict[str, Any]:
        await self.initialize()
        shared_device = await self.repository.get_device(device_id)
        if shared_device is not None:
            async with self._lock:
                self.devices[device_id] = shared_device
        return await super().authenticate_device(device_id, token)

    async def _refresh_device_runtime_state(self, device_id: str) -> bool:
        """Refresh one Device route without reloading another worker's sockets."""

        state = await self.repository.load_device_runtime_state(device_id)
        shared_device = state.get("device")
        if not isinstance(shared_device, dict):
            return False
        async with self._lock:
            existing_device = self.devices.get(device_id)
            if existing_device is not None:
                shared_device["_connection_generation"] = max(
                    int(shared_device.get("_connection_generation", 0)),
                    int(existing_device.get("_connection_generation", 0)),
                )
            self.devices[device_id] = shared_device

            self.bindings = {
                binding_id: binding
                for binding_id, binding in self.bindings.items()
                if str(binding["device_id"]) != device_id
            }
            self.bindings.update(
                {
                    str(binding["binding_id"]): binding
                    for binding in state.get("bindings", [])
                }
            )
            self.commands = {
                command_id: command
                for command_id, command in self.commands.items()
                if str(command["device_id"]) != device_id
            }
            for command in state.get("commands", []):
                command["_durable_ready"] = True
                self.commands[str(command["command_id"])] = command
            self.agent_task_results = {
                task_id: result
                for task_id, result in self.agent_task_results.items()
                if str(result["device_id"]) != device_id
            }
            self.agent_task_results.update(
                {
                    str(result["task_id"]): result
                    for result in state.get("agent_task_results", [])
                }
            )
            self.events = [
                event
                for event in self.events
                if str(event["device_id"]) != device_id
            ] + list(state.get("events", []))
            self._persisted_event_ids.update(
                str(event["event_id"]) for event in state.get("events", [])
            )
            self._rebuild_event_watermarks()
        return True

    async def connect_device(self, device_id: str, websocket: Any) -> int:
        await self.initialize()
        if self._draining:
            raise ConnectorError(503, "Gateway worker is draining")
        await self._refresh_device_runtime_state(device_id)
        result = await super().connect_device(device_id, websocket)
        try:
            fencing_token = await self.repository.claim_device_connection(
                device_id,
                self.instance_id,
                self.connection_lease_seconds,
            )
        except Exception:
            await super().disconnect_device(device_id, websocket)
            raise
        self._connection_lease_tokens[websocket] = fencing_token
        shared_device = await self.repository.get_device(device_id)
        if shared_device is not None:
            async with self._lock:
                device = self.devices[device_id]
                device["outbound_sequence"] = max(
                    int(device.get("outbound_sequence", 0)),
                    int(shared_device.get("outbound_sequence", 0)),
                )
                device["event_ack_watermark"] = max(
                    int(device.get("event_ack_watermark", 0)),
                    int(shared_device.get("event_ack_watermark", 0)),
                )
                device["event_pending_sequences"] = sorted(
                    {
                        int(value)
                        for value in device.get("event_pending_sequences", [])
                    }
                    | {
                        int(value)
                        for value in shared_device.get(
                            "event_pending_sequences",
                            [],
                        )
                    }
                )
        try:
            await self._persist_current(
                device_ids={device_id},
                connection_fence=(device_id, fencing_token),
            )
        except Exception:
            self._connection_lease_tokens.pop(websocket, None)
            await self.repository.release_device_connection(
                device_id,
                self.instance_id,
                fencing_token,
            )
            await super().disconnect_device(device_id, websocket)
            raise
        return result

    async def disconnect_device(self, device_id: str, websocket: Any) -> None:
        await self.initialize()
        fencing_token = self._connection_lease_tokens.pop(websocket, None)
        await super().disconnect_device(device_id, websocket)
        if fencing_token is not None:
            async with self._lock:
                device = copy.deepcopy(self.devices[device_id])
            released = await self.repository.release_device_connection_and_save_device(
                device_id,
                self.instance_id,
                fencing_token,
                device,
            )
            if released:
                # The Device projection was committed under the same fence as
                # lease release. Persist only the appended disconnect audit.
                await self._persist_current(full=False)

    async def assert_active_connection(
        self,
        device_id: str,
        websocket: Any,
        generation: Optional[int] = None,
    ) -> None:
        await self.initialize()
        shared_device = await self.repository.get_device(device_id)
        if shared_device is not None and shared_device.get("revoked_at"):
            raise ConnectorError(410, "Device has been revoked")
        await super().assert_active_connection(device_id, websocket, generation)
        fencing_token = self._connection_lease_tokens.get(websocket)
        if fencing_token is None or not await self.repository.is_device_connection_owner(
            device_id,
            self.instance_id,
            fencing_token,
        ):
            raise ConnectionReplacedError(
                "WebSocket was replaced by a connection on another Gateway instance",
            )

    async def _can_deliver_to_connection(
        self,
        device_id: str,
        websocket: Any,
        generation: int,
    ) -> bool:
        fencing_token = self._connection_lease_tokens.get(websocket)
        if fencing_token is None:
            return False
        return await self.repository.is_device_connection_owner(
            device_id,
            self.instance_id,
            fencing_token,
        )

    def _transition_fence(
        self,
        device_id: str,
        expected_generation: Optional[int],
    ) -> tuple[str, int] | None:
        if expected_generation is None:
            return None
        websocket = self.connections.get(device_id)
        fencing_token = (
            self._connection_lease_tokens.get(websocket)
            if websocket is not None
            else None
        )
        if fencing_token is None:
            raise ConnectionReplacedError(
                "WebSocket was replaced by a connection on another Gateway instance",
            )
        return device_id, fencing_token

    async def apply_hello(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        await self.initialize()
        result = await super().apply_hello(device_id, payload, expected_generation)
        async with self._lock:
            binding_ids = {
                str(item["binding_id"])
                for item in self.bindings.values()
                if item["device_id"] == device_id
            }
        await self._persist_current(
            device_ids={device_id},
            binding_ids=binding_ids,
            connection_fence=self._transition_fence(
                device_id,
                expected_generation,
            ),
        )
        return result

    async def heartbeat(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> None:
        await self.initialize()
        await super().heartbeat(device_id, payload, expected_generation)
        if expected_generation is not None:
            websocket = self.connections.get(device_id)
            fencing_token = (
                self._connection_lease_tokens.get(websocket)
                if websocket is not None
                else None
            )
            if fencing_token is None or not await self.repository.renew_device_connection(
                device_id,
                self.instance_id,
                fencing_token,
                self.connection_lease_seconds,
            ):
                raise ConnectionReplacedError(
                    "WebSocket was replaced by a connection on another Gateway instance",
                )
        await self._persist_current(
            device_ids={device_id},
            connection_fence=self._transition_fence(
                device_id,
                expected_generation,
            ),
        )

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
        await self._persist_current(
            device_ids={device_id},
            runtime_device_ids={device_id},
            connection_fence=self._transition_fence(
                device_id,
                expected_generation,
            ),
        )
        return result

    async def list_devices(
        self, owner_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        await self.initialize()
        shared_devices = await self.repository.list_devices(owner_id)
        active_device_ids = {
            str(shared_device["device_id"])
            for shared_device in shared_devices
            if shared_device.pop("_has_active_connection", False)
        }
        async with self._lock:
            for shared_device in shared_devices:
                device_id = str(shared_device["device_id"])
                existing = self.devices.get(device_id)
                if existing is not None:
                    shared_device["_connection_generation"] = max(
                        int(shared_device.get("_connection_generation", 0)),
                        int(existing.get("_connection_generation", 0)),
                    )
                self.devices[device_id] = shared_device
            devices = [
                self._public_device(device)
                for device in self.devices.values()
                if owner_id is None or device["owner_id"] == owner_id
            ]
            for device in devices:
                if (
                    not device.get("revoked_at")
                    and str(device["device_id"]) in active_device_ids
                ):
                    device["status"] = "online"
            return sorted(
                devices,
                key=lambda value: value["created_at"],
                reverse=True,
            )

    async def get_device(self, device_id: str) -> dict[str, Any]:
        await self.initialize()
        await self._refresh_device_runtime_state(device_id)
        device = await super().get_device(device_id)
        if (
            not device.get("revoked_at")
            and await self.repository.has_active_device_connection(device_id)
        ):
            device["status"] = "online"
        return device

    async def revoke_device(self, device_id: str, owner_id: str) -> dict[str, Any]:
        await self.initialize()
        await self._refresh_device_runtime_state(device_id)
        result = await super().revoke_device(device_id, owner_id)
        async with self._lock:
            binding_ids = {
                str(item["binding_id"])
                for item in self.bindings.values()
                if item["device_id"] == device_id
            }
        await self._persist_current(
            device_ids={device_id},
            binding_ids=binding_ids,
        )
        return result

    async def create_binding(
        self,
        device_id: str,
        runtime_id: str,
        agent_id: Optional[str],
        display_name: Optional[str],
        working_directory: Optional[str] = None,
    ) -> dict[str, Any]:
        await self.initialize()
        await self._refresh_device_runtime_state(device_id)
        result = await super().create_binding(
            device_id,
            runtime_id,
            agent_id,
            display_name,
            working_directory,
        )
        await self._persist_current(binding_ids={str(result["binding_id"])})
        return result

    async def list_bindings(
        self, device_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        await self.initialize()
        shared_bindings = await self.repository.list_bindings(device_id)
        async with self._lock:
            if device_id is None:
                self.bindings = {
                    str(binding["binding_id"]): binding
                    for binding in shared_bindings
                }
            else:
                self.bindings = {
                    binding_id: binding
                    for binding_id, binding in self.bindings.items()
                    if str(binding["device_id"]) != device_id
                }
                self.bindings.update(
                    {
                        str(binding["binding_id"]): binding
                        for binding in shared_bindings
                    }
                )
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
        shared_binding = await self.repository.get_binding(binding_id)
        if shared_binding is not None:
            await self._refresh_device_runtime_state(
                str(shared_binding["device_id"])
            )
        if idempotency_key:
            shared = await self.repository.get_command_by_idempotency_key(
                binding_id,
                idempotency_key,
            )
            if shared is not None:
                shared["_durable_ready"] = True
                async with self._lock:
                    self.commands[str(shared["command_id"])] = shared
        result = await super().queue_command(
            binding_id,
            action,
            payload,
            idempotency_key,
            expires_in_seconds,
        )
        return result

    async def _prepare_command_delivery(self, command_id: str) -> None:
        # The full queued command, its idempotency key and its audit record must
        # be committed before the active WebSocket can observe the command.
        async with self._lock:
            command = self.commands.get(command_id)
            if command is not None and command.get("_durable_ready", False):
                return
        await self._persist_current(command_ids={command_id})

    async def _prepare_outbound_sequence(
        self,
        device_id: str,
        sequence: int,
    ) -> None:
        # The durable resume cursor must never lag a frame already visible to
        # the Connector. Persist the reserved sequence before the socket write.
        async with self._lock:
            websocket = self.connections.get(device_id)
        fencing_token = (
            self._connection_lease_tokens.get(websocket)
            if websocket is not None
            else None
        )
        if fencing_token is None or not await self.repository.save_outbound_sequence_for_connection_owner(
            device_id,
            self.instance_id,
            fencing_token,
            sequence,
        ):
            raise ConnectionReplacedError(
                "WebSocket was replaced by a connection on another Gateway instance",
            )

    async def _commit_command_delivery(
        self,
        device_id: str,
        websocket: Any,
        generation: int,
        command: dict[str, Any],
    ) -> None:
        fencing_token = self._connection_lease_tokens.get(websocket)
        if fencing_token is None:
            return
        try:
            committed = await self.repository.save_command_for_connection_owner(
                device_id,
                self.instance_id,
                fencing_token,
                command,
            )
        except Exception:
            await self._restore_local_command_for_replay(command)
            raise
        if committed:
            return
        # A takeover or a faster ACK won the shared-state race. Keep the local
        # view replayable until the next shared refresh rather than treating a
        # stale delivery commit as authoritative.
        await self._restore_local_command_for_replay(command)

    async def _restore_local_command_for_replay(
        self,
        command: dict[str, Any],
    ) -> None:
        async with self._lock:
            current = self.commands.get(str(command["command_id"]))
            if current is not None and current.get("status") == "delivered":
                current["status"] = "queued"
                current["delivered_at"] = None

    async def deliver_pending(self, device_id: str) -> int:
        await self.initialize()
        return await super().deliver_pending(device_id)

    async def route_shared_commands_once(self, *, limit: int = 100) -> int:
        """Pull queued Commands whose Device lease belongs to this instance."""

        if limit < 1 or limit > 500:
            raise ValueError("Shared Command route limit must be between 1 and 500")
        await self.initialize()
        async with self._shared_command_route_lock:
            routes = (
                await self.repository.list_queued_command_routes_for_connection_owner(
                    self.instance_id,
                    limit,
                )
            )
            route_device_ids: set[str] = set()
            async with self._lock:
                for route in routes:
                    command = route["command"]
                    binding = route["binding"]
                    command_id = str(command["command_id"])
                    current = self.commands.get(command_id)
                    if current is not None and current.get("status") != "queued":
                        continue
                    command["_durable_ready"] = True
                    self.bindings[str(binding["binding_id"])] = binding
                    self.commands[command_id] = command
                    route_device_ids.add(str(command["device_id"]))
            delivered = 0
            for device_id in sorted(route_device_ids):
                delivered += await self.deliver_pending(device_id)
            return delivered

    async def notify_task_available(
        self,
        binding_id: str,
        payload: dict[str, Any],
    ) -> bool:
        await self.initialize()
        result = await super().notify_task_available(binding_id, payload)
        if result:
            binding = next(
                (
                    item
                    for item in await self.list_bindings()
                    if item["binding_id"] == binding_id
                ),
                None,
            )
            if binding is not None:
                device_id = str(binding["device_id"])
                websocket = self.connections.get(device_id)
                fencing_token = (
                    self._connection_lease_tokens.get(websocket)
                    if websocket is not None
                    else None
                )
                if fencing_token is None:
                    raise ConnectionReplacedError(
                        "WebSocket was replaced by a connection on another Gateway instance",
                    )
                await self._persist_current(
                    device_ids={device_id},
                    connection_fence=(device_id, fencing_token),
                )
        return result

    async def observe_inbound_sequence(
        self,
        device_id: str,
        sequence: Optional[int],
        expected_generation: Optional[int] = None,
    ) -> None:
        await self.initialize()
        await super().observe_inbound_sequence(device_id, sequence, expected_generation)
        await self._persist_current(
            device_ids={device_id},
            connection_fence=self._transition_fence(
                device_id,
                expected_generation,
            ),
        )

    async def resume_transport(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        await self.initialize()
        result = await super().resume_transport(
            device_id,
            payload,
            expected_generation,
        )
        await self._persist_current(
            device_ids={device_id},
            connection_fence=self._transition_fence(
                device_id,
                expected_generation,
            ),
        )
        return result

    def _result_ready_for_transport_ack(self, record: dict[str, Any]) -> bool:
        if self._agent_task_result_sink is None:
            return True
        return bool(record.get("arena_sink_accepted_at"))

    async def acknowledge_task_available(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        await self.initialize()
        result = await super().acknowledge_task_available(
            device_id,
            payload,
            expected_generation,
        )
        await self._persist_current(
            device_ids={device_id},
            connection_fence=self._transition_fence(
                device_id,
                expected_generation,
            ),
        )
        return result

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
        await self._persist_current(
            command_ids={str(result["command_id"])},
            binding_ids={str(result["binding_id"])},
            connection_fence=self._transition_fence(
                device_id,
                expected_generation,
            ),
        )
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
        binding_id = str(payload.get("binding_id", ""))
        await self._persist_current(
            device_ids={device_id},
            binding_ids={binding_id} if binding_id else set(),
            connection_fence=self._transition_fence(
                device_id,
                expected_generation,
            ),
        )
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
        result_payload = payload.get("result")
        task_id = (
            str(result_payload.get("taskId", ""))
            if isinstance(result_payload, dict)
            else ""
        )
        await self._persist_current(
            result_task_ids={task_id} if task_id else set(),
            connection_fence=self._transition_fence(
                device_id,
                expected_generation,
            ),
        )
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
            if task_id:
                async with self._lock:
                    accepted = self.agent_task_results.get(task_id)
                    if accepted is not None and not accepted.get(
                        "arena_sink_accepted_at"
                    ):
                        accepted["arena_sink_accepted_at"] = iso(utc_now())
                await self._persist_current(result_task_ids={task_id})
        # The Connector receives its result acknowledgement only after both
        # the Gateway durable inbox and the configured Arena Sink succeed.
        return result

    async def list_events(
        self, binding_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        await self.initialize()
        bounded_limit = max(1, min(limit, 1000))
        if not any(
            binding["binding_id"] == binding_id
            for binding in await self.repository.list_bindings()
        ):
            raise ConnectorError(404, "Binding not found")
        return await self.repository.list_events(binding_id, bounded_limit)

    async def list_commands(
        self, binding_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        await self.initialize()
        bounded_limit = max(1, min(limit, 500))
        if not any(
            binding["binding_id"] == binding_id
            for binding in await self.repository.list_bindings()
        ):
            raise ConnectorError(404, "Binding not found")
        return await self.repository.list_commands(binding_id, bounded_limit)

    async def list_audit(
        self, limit: int = 200, owner_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        await self.initialize()
        return await self.repository.list_audit(
            max(1, min(limit, 1000)),
            owner_id,
        )
