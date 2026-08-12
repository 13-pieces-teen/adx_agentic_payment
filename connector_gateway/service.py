"""In-memory MVP implementation of the platform Connector control plane.

The service owns device pairing, runtime inventory, bindings, a typed command
queue and an append-only event/audit view.  Storage is intentionally isolated
behind this class so a durable repository can replace the dictionaries without
changing the HTTP or WebSocket protocol.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from arena_agent_contracts import AgentTaskResultV1, ArenaAgentTaskV1
from pydantic import ValidationError

from .models import (
    BindingStatus,
    CommandAction,
    CommandStatus,
    PairingStatus,
    RuntimeInventoryItem,
)


class ConnectorError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ConnectionReplacedError(ConnectorError):
    def __init__(self, detail: str = "WebSocket was replaced by a newer connection"):
        super().__init__(409, detail)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def digest_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class ConnectorGateway:
    """Application-scoped Connector state and policy enforcement."""

    protocol_version = "1.0"
    heartbeat_lease_seconds = 45

    _payload_fields: dict[CommandAction, set[str]] = {
        CommandAction.RUNTIME_PROBE: set(),
        CommandAction.SESSION_START: {
            "working_directory",
            "initial_prompt",
            "environment_refs",
        },
        CommandAction.TASK_DISPATCH: {
            "session_id",
            "prompt",
            "request_id",
            "task",
        },
        CommandAction.TASK_CANCEL: {"session_id", "request_id"},
        CommandAction.SESSION_STOP: {"session_id", "reason"},
        CommandAction.SESSION_RESUME: {"session_id"},
    }
    _required_payload_fields: dict[CommandAction, set[str]] = {
        CommandAction.RUNTIME_PROBE: set(),
        CommandAction.SESSION_START: {"working_directory"},
        CommandAction.TASK_DISPATCH: {"session_id"},
        CommandAction.TASK_CANCEL: {"session_id", "request_id"},
        CommandAction.SESSION_STOP: {"session_id"},
        CommandAction.SESSION_RESUME: {"session_id"},
    }
    _forbidden_payload_fields = {
        "shell",
        "command",
        "cmd",
        "argv",
        "executable",
        "executable_path",
        "script",
        "conversation_id",
        "resume_token",
    }
    _session_owned_actions = {
        CommandAction.TASK_DISPATCH,
        CommandAction.TASK_CANCEL,
        CommandAction.SESSION_STOP,
        CommandAction.SESSION_RESUME,
    }
    _sensitive_key_pattern = re.compile(
        r"(api[_-]?key|access[_-]?token|authorization|cookie|password|secret|private[_-]?key)",
        re.IGNORECASE,
    )
    _sensitive_text_patterns = (
        re.compile(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b", re.IGNORECASE),
        re.compile(
            r"(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]+",
            re.IGNORECASE,
        ),
    )

    def __init__(
        self,
        verification_uri: Optional[str] = None,
        max_pending_pairings: int = 500,
    ) -> None:
        public_app_url = os.getenv("ADX_PUBLIC_APP_URL", "http://localhost:3000")
        self.verification_uri = verification_uri or (
            public_app_url.rstrip("/") + "/agents#connect"
        )
        self.pairings: dict[str, dict[str, Any]] = {}
        self.pairings_by_device_code: dict[str, str] = {}
        self.devices: dict[str, dict[str, Any]] = {}
        self.bindings: dict[str, dict[str, Any]] = {}
        self.commands: dict[str, dict[str, Any]] = {}
        self.agent_task_results: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []
        self.connections: dict[str, Any] = {}
        self.connection_sets: dict[str, set[Any]] = {}
        self.connection_ready_generations: dict[str, int] = {}
        self._send_locks: dict[str, asyncio.Lock] = {}
        # Connector event sequences are monotonic per device so a single
        # cumulative ACK can safely compact the device's durable outbox.
        self.event_ack_watermarks: dict[str, int] = {}
        self.event_pending_sequences: dict[str, set[int]] = {}
        self.max_pending_pairings = max_pending_pairings
        self._lock = asyncio.Lock()

    async def create_pairing(
        self, requested_owner_id: Optional[str], device_name: str
    ) -> dict[str, Any]:
        async with self._lock:
            now = utc_now()
            self._remove_expired_pairings(now)
            open_pairings = sum(
                record["status"]
                in {
                    PairingStatus.PENDING.value,
                    PairingStatus.APPROVED.value,
                }
                for record in self.pairings.values()
            )
            if open_pairings >= self.max_pending_pairings:
                raise ConnectorError(
                    503,
                    "Connector pairing capacity is temporarily full; retry later",
                )
            pairing_id = new_id("pair")
            user_code = self._unique_user_code()
            device_code = secrets.token_urlsafe(32)
            record = {
                "pairing_id": pairing_id,
                "owner_id": None,
                "requested_owner_id": requested_owner_id,
                "device_name": device_name,
                "user_code": user_code,
                "device_code_hash": digest_secret(device_code),
                "status": PairingStatus.PENDING.value,
                "created_at": iso(now),
                "expires_at": iso(now + timedelta(minutes=10)),
                "approved_at": None,
                "consumed_at": None,
                "device_id": None,
            }
            self.pairings[user_code] = record
            self.pairings_by_device_code[digest_secret(device_code)] = user_code
            self._append_audit(
                "pairing.created", "connector", {"pairing_id": pairing_id}
            )
            return {
                **self._public_pairing(record),
                "device_code": device_code,
                "verification_uri": self.verification_uri,
            }

    def _remove_expired_pairings(self, now: datetime) -> int:
        """Bound unauthenticated pairing state before taking another request."""

        removed = 0
        for user_code, record in list(self.pairings.items()):
            if record["status"] not in {
                PairingStatus.PENDING.value,
                PairingStatus.APPROVED.value,
                PairingStatus.EXPIRED.value,
            }:
                continue
            if self._parse_time(record["expires_at"]) > now:
                continue
            device_code_hash = record.get("device_code_hash")
            if device_code_hash:
                self.pairings_by_device_code.pop(str(device_code_hash), None)
            self.pairings.pop(user_code, None)
            removed += 1
        return removed

    async def approve_pairing(self, user_code: str, owner_id: str) -> dict[str, Any]:
        async with self._lock:
            record = self._get_pairing(user_code)
            self._refresh_pairing_expiry(record)
            if record["status"] == PairingStatus.EXPIRED.value:
                raise ConnectorError(410, "Pairing code expired")
            if record["status"] == PairingStatus.APPROVED.value:
                raise ConnectorError(409, "Pairing code already approved")
            if record["status"] == PairingStatus.CONSUMED.value:
                raise ConnectorError(409, "Pairing code already consumed")
            if record["owner_id"] is not None and record["owner_id"] != owner_id:
                raise ConnectorError(403, "Pairing belongs to another owner")
            record["owner_id"] = owner_id
            record["status"] = PairingStatus.APPROVED.value
            record["approved_at"] = iso(utc_now())
            self._append_audit(
                "pairing.approved", owner_id, {"pairing_id": record["pairing_id"]}
            )
            return self._public_pairing(record)

    async def exchange_pairing(self, device_code: str) -> dict[str, Any]:
        async with self._lock:
            code_hash = digest_secret(device_code)
            user_code = self.pairings_by_device_code.get(code_hash)
            if not user_code:
                raise ConnectorError(401, "Invalid device code")
            record = self._get_pairing(user_code)
            self._refresh_pairing_expiry(record)
            if record["status"] == PairingStatus.PENDING.value:
                raise ConnectorError(428, "Pairing is waiting for user approval")
            if record["status"] == PairingStatus.EXPIRED.value:
                raise ConnectorError(410, "Pairing code expired")
            if record["status"] == PairingStatus.CONSUMED.value:
                raise ConnectorError(409, "Pairing code already consumed")

            device_id = new_id("device")
            device_token = secrets.token_urlsafe(48)
            now = utc_now()
            device = {
                "device_id": device_id,
                "owner_id": record["owner_id"],
                "name": record["device_name"],
                "status": "offline",
                "token_hash": digest_secret(device_token),
                "connector_version": None,
                "connector_started_at": None,
                "platform": None,
                "hostname": None,
                "protocol_version": None,
                "created_at": iso(now),
                "connected_at": None,
                "last_seen_at": None,
                "runtimes": [],
                "binding_epoch": 1,
                "outbound_sequence": 0,
                "last_inbound_sequence": None,
                "event_ack_watermark": 0,
                "event_pending_sequences": [],
                "_connection_generation": 0,
            }
            self.devices[device_id] = device
            record["status"] = PairingStatus.CONSUMED.value
            record["consumed_at"] = iso(now)
            record["device_id"] = device_id
            self.pairings_by_device_code.pop(code_hash, None)
            self._append_audit(
                "device.enrolled",
                record["owner_id"],
                {"device_id": device_id, "pairing_id": record["pairing_id"]},
            )
            return {
                "device_id": device_id,
                "device_token": device_token,
                "token_type": "Device",
                "ws_url": "/api/connectors/ws",
                "protocol_version": self.protocol_version,
            }

    async def authenticate_device(self, device_id: str, token: str) -> dict[str, Any]:
        async with self._lock:
            device = self.devices.get(device_id)
            if not device:
                raise ConnectorError(401, "Invalid device credentials")
            if device.get("revoked_at"):
                raise ConnectorError(410, "Device has been revoked")
            if not hmac.compare_digest(device["token_hash"], digest_secret(token)):
                raise ConnectorError(401, "Invalid device credentials")
            return device

    async def connect_device(self, device_id: str, websocket: Any) -> int:
        send_lock = self._send_locks.setdefault(device_id, asyncio.Lock())
        async with send_lock:
            previous = None
            async with self._lock:
                device = self._get_device(device_id)
                self._ensure_not_revoked(device)
                now = iso(utc_now())
                device["status"] = "online"
                device["connected_at"] = now
                device["last_seen_at"] = now
                device["_connection_generation"] += 1
                generation = int(device["_connection_generation"])
                previous = self.connections.get(device_id)
                self.connections[device_id] = websocket
                self.connection_ready_generations.pop(device_id, None)
                self.connection_sets.setdefault(device_id, set()).add(websocket)
                # A command may have reached the previous socket immediately before
                # it was lost. Requeue only the unacknowledged delivery; the
                # idempotency key makes the Connector-side replay safe.
                for command in self.commands.values():
                    if (
                        command["device_id"] == device_id
                        and command["status"] == CommandStatus.DELIVERED.value
                    ):
                        command["status"] = CommandStatus.QUEUED.value
                        command["updated_at"] = now
                self._append_audit(
                    "device.connected",
                    device_id,
                    {"device_id": device_id, "generation": generation},
                )
            if previous is not None and previous is not websocket:
                try:
                    await previous.close(
                        code=4409, reason="Replaced by a newer connection"
                    )
                except Exception:
                    pass
            return generation

    async def disconnect_device(self, device_id: str, websocket: Any) -> None:
        async with self._lock:
            sockets = self.connection_sets.get(device_id)
            if sockets is not None:
                sockets.discard(websocket)
                if not sockets:
                    self.connection_sets.pop(device_id, None)
            if self.connections.get(device_id) is websocket:
                self.connections.pop(device_id, None)
                self.connection_ready_generations.pop(device_id, None)
                device = self.devices.get(device_id)
                if device:
                    device["status"] = "offline"
                    self._append_audit(
                        "device.disconnected", device_id, {"device_id": device_id}
                    )

    async def apply_hello(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        async with self._lock:
            device = self._get_device(device_id)
            self._ensure_not_revoked(device)
            self._ensure_connection_generation(device, expected_generation)
            protocol_version = str(payload.get("protocol_version", ""))
            if protocol_version != self.protocol_version:
                raise ConnectorError(
                    409,
                    f"Unsupported protocol version {protocol_version!r}; expected {self.protocol_version}",
                )
            for field in (
                "connector_version",
                "platform",
                "hostname",
                "protocol_version",
            ):
                value = payload.get(field)
                if value is not None:
                    device[field] = str(value)[:256]
            started_at = str(payload.get("started_at", "")).strip()[:128]
            previous_started_at = device.get("connector_started_at")
            if started_at:
                device["connector_started_at"] = started_at
                if previous_started_at and previous_started_at != started_at:
                    reset_at = iso(utc_now())
                    reset_bindings = 0
                    for binding in self.bindings.values():
                        if binding["device_id"] == device_id:
                            binding["session_generation"] = (
                                int(binding.get("session_generation", 0)) + 1
                            )
                            if binding["status"] != BindingStatus.STOPPED.value and (
                                binding.get("last_session_id")
                                or binding.get("last_task_id")
                            ):
                                binding["status"] = BindingStatus.DEGRADED.value
                            binding["last_session_id"] = None
                            binding["last_task_id"] = None
                            binding["updated_at"] = reset_at
                            reset_bindings += 1
                    self._append_audit(
                        "device.connector_restarted",
                        device_id,
                        {
                            "device_id": device_id,
                            "reset_bindings": reset_bindings,
                        },
                    )
            device["last_seen_at"] = iso(utc_now())
            return self._public_device(device)

    async def heartbeat(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> None:
        async with self._lock:
            device = self._get_device(device_id)
            self._ensure_not_revoked(device)
            self._ensure_connection_generation(device, expected_generation)
            device["last_seen_at"] = iso(utc_now())
            device["status"] = "online"
            if "active_sessions" in payload:
                device["active_sessions"] = max(0, int(payload["active_sessions"]))

    async def update_inventory(
        self,
        device_id: str,
        runtimes: list[RuntimeInventoryItem],
        host: Optional[dict[str, Any]] = None,
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        async with self._lock:
            device = self._get_device(device_id)
            self._ensure_not_revoked(device)
            self._ensure_connection_generation(device, expected_generation)
            normalized = [self._model_dump(item) for item in runtimes]
            runtime_ids = [runtime["runtime_id"] for runtime in normalized]
            if len(runtime_ids) != len(set(runtime_ids)):
                raise ConnectorError(422, "runtime_id values must be unique per device")
            device["runtimes"] = normalized
            device["last_seen_at"] = iso(utc_now())
            if isinstance(host, dict):
                if host.get("hostname"):
                    device["hostname"] = str(host["hostname"])[:256]
                os_name = str(host.get("os", ""))[:128]
                architecture = str(host.get("architecture", ""))[:128]
                if os_name or architecture:
                    device["platform"] = "/".join(
                        value for value in (os_name, architecture) if value
                    )
                if host.get("connector_version"):
                    device["connector_version"] = str(host["connector_version"])[:256]
            self._append_audit(
                "inventory.updated",
                device_id,
                {"device_id": device_id, "runtime_count": len(normalized)},
            )
            return self._public_device(device)

    async def list_devices(
        self, owner_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        async with self._lock:
            devices = [
                self._public_device(device)
                for device in self.devices.values()
                if owner_id is None or device["owner_id"] == owner_id
            ]
            return sorted(devices, key=lambda value: value["created_at"], reverse=True)

    async def get_device(self, device_id: str) -> dict[str, Any]:
        async with self._lock:
            return self._public_device(self._get_device(device_id))

    async def revoke_device(self, device_id: str, owner_id: str) -> dict[str, Any]:
        send_lock = self._send_locks.setdefault(device_id, asyncio.Lock())
        async with send_lock:
            websockets: list[Any] = []
            async with self._lock:
                device = self._get_device(device_id)
                if device["owner_id"] != owner_id:
                    raise ConnectorError(403, "Device belongs to another owner")
                if device.get("revoked_at"):
                    return self._public_device(device)
                device["revoked_at"] = iso(utc_now())
                device["status"] = "revoked"
                device["token_hash"] = digest_secret(secrets.token_urlsafe(48))
                self.connections.pop(device_id, None)
                websockets = list(self.connection_sets.pop(device_id, set()))
                device["binding_epoch"] += 1
                device["_connection_generation"] += 1
                for binding in self.bindings.values():
                    if binding["device_id"] == device_id:
                        binding["status"] = BindingStatus.STOPPED.value
                        binding["binding_epoch"] = device["binding_epoch"]
                        binding["updated_at"] = device["revoked_at"]
                self._append_audit("device.revoked", owner_id, {"device_id": device_id})
                public = self._public_device(device)
            for websocket in websockets:
                try:
                    await websocket.close(code=4403, reason="Device revoked")
                except Exception:
                    pass
            return public

    async def assert_active_connection(
        self, device_id: str, websocket: Any, generation: Optional[int] = None
    ) -> None:
        async with self._lock:
            device = self._get_device(device_id)
            self._ensure_not_revoked(device)
            if self.connections.get(device_id) is not websocket or (
                generation is not None
                and device["_connection_generation"] != generation
            ):
                raise ConnectionReplacedError(
                    "WebSocket is no longer an active device connection"
                )

    async def mark_transport_ready(
        self,
        device_id: str,
        expected_generation: int,
    ) -> None:
        async with self._lock:
            device = self._get_device(device_id)
            self._ensure_not_revoked(device)
            self._ensure_connection_generation(device, expected_generation)
            self.connection_ready_generations[device_id] = expected_generation

    async def send_active_message(
        self,
        device_id: str,
        websocket: Any,
        generation: int,
        payload: dict[str, Any],
    ) -> None:
        """Serialize every outbound frame with connection handover and revoke."""
        send_lock = self._send_locks.setdefault(device_id, asyncio.Lock())
        async with send_lock:
            await self.assert_active_connection(device_id, websocket, generation)
            await websocket.send_json(payload)

    async def create_binding(
        self,
        device_id: str,
        runtime_id: str,
        agent_id: Optional[str],
        display_name: Optional[str],
        working_directory: Optional[str] = None,
    ) -> dict[str, Any]:
        if working_directory is not None and (
            not working_directory.strip() or len(working_directory) > 2048
        ):
            raise ConnectorError(
                422,
                "Binding working directory must be a non-empty string "
                "no longer than 2048 characters",
            )
        async with self._lock:
            device = self._get_device(device_id)
            if device.get("revoked_at"):
                raise ConnectorError(410, "Device has been revoked")
            runtime = next(
                (
                    item
                    for item in device["runtimes"]
                    if item["runtime_id"] == runtime_id and item.get("available", True)
                ),
                None,
            )
            if not runtime:
                raise ConnectorError(404, "Runtime is not present or unavailable")
            for binding in self.bindings.values():
                if (
                    binding["device_id"] == device_id
                    and binding["runtime_id"] == runtime_id
                ):
                    existing_directory = binding.get("working_directory")
                    if working_directory is not None and existing_directory is None:
                        binding["working_directory"] = working_directory
                        binding["updated_at"] = iso(utc_now())
                        self._append_audit(
                            "binding.working_directory_frozen",
                            device["owner_id"],
                            {
                                "binding_id": binding["binding_id"],
                                "device_id": device_id,
                                "runtime_id": runtime_id,
                            },
                        )
                    elif (
                        working_directory is not None
                        and existing_directory != working_directory
                    ):
                        raise ConnectorError(
                            409,
                            "Binding working directory is already frozen",
                        )
                    return dict(binding)

            now = iso(utc_now())
            binding_id = new_id("binding")
            binding = {
                "binding_id": binding_id,
                "device_id": device_id,
                "runtime_id": runtime_id,
                "runtime_kind": runtime["kind"],
                "agent_id": agent_id or new_id("agent"),
                "display_name": display_name or runtime["display_name"],
                "working_directory": working_directory,
                "status": BindingStatus.AVAILABLE.value,
                "binding_epoch": device["binding_epoch"],
                "session_generation": 0,
                "created_at": now,
                "updated_at": now,
                "last_session_id": None,
                "last_task_id": None,
            }
            self.bindings[binding_id] = binding
            self._append_audit(
                "binding.created",
                device["owner_id"],
                {
                    "binding_id": binding_id,
                    "device_id": device_id,
                    "runtime_id": runtime_id,
                },
            )
            return dict(binding)

    async def list_bindings(
        self, device_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        async with self._lock:
            values = [
                dict(binding)
                for binding in self.bindings.values()
                if device_id is None or binding["device_id"] == device_id
            ]
            return sorted(values, key=lambda value: value["created_at"], reverse=True)

    async def queue_command(
        self,
        binding_id: str,
        action: CommandAction,
        payload: dict[str, Any],
        idempotency_key: Optional[str],
        expires_in_seconds: int,
    ) -> dict[str, Any]:
        payload = dict(payload)
        if (
            action == CommandAction.TASK_DISPATCH
            and "task" not in payload
            and "request_id" not in payload
        ):
            payload["request_id"] = idempotency_key or new_id("request")
        self._validate_command_payload(action, payload)
        request_fingerprint = hashlib.sha256(
            json.dumps(
                {"action": action.value, "payload": payload},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        selected_command: dict[str, Any] | None = None
        async with self._lock:
            binding = self.bindings.get(binding_id)
            if not binding:
                raise ConnectorError(404, "Binding not found")
            if action in self._session_owned_actions:
                session_id = str(payload["session_id"])
                owned_session_id = binding.get("last_session_id")
                if not owned_session_id:
                    raise ConnectorError(
                        409, "Binding has no active Connector-owned session"
                    )
                if session_id != owned_session_id:
                    raise ConnectorError(
                        409, "Session does not belong to this runtime binding"
                    )
            device = self._get_device(binding["device_id"])
            if device.get("revoked_at"):
                raise ConnectorError(410, "Device has been revoked")
            runtime = next(
                (
                    item
                    for item in device["runtimes"]
                    if item["runtime_id"] == binding["runtime_id"]
                ),
                None,
            )
            if action != CommandAction.RUNTIME_PROBE and (
                runtime is None or not runtime.get("available", True)
            ):
                raise ConnectorError(
                    409,
                    f"Runtime {binding['runtime_id']} is no longer available",
                )
            capabilities = set(runtime.get("capabilities", [])) if runtime else set()
            if (
                action != CommandAction.RUNTIME_PROBE
                and action.value not in capabilities
            ):
                raise ConnectorError(
                    409,
                    f"Runtime {binding['runtime_id']} does not advertise {action.value}",
                )
            if (
                action == CommandAction.TASK_DISPATCH
                and isinstance(payload.get("task"), dict)
                and runtime is not None
            ):
                issues = self._arena_runtime_readiness_issues(runtime)
                if issues:
                    safe_issues = ", ".join(str(issue)[:128] for issue in issues[:4])
                    raise ConnectorError(
                        409,
                        f"Runtime {binding['runtime_id']} is not ready for Arena "
                        f"execution: {safe_issues}",
                    )
            if idempotency_key:
                matching_commands = [
                    command
                    for command in self.commands.values()
                    if command["binding_id"] == binding_id
                    and command["idempotency_key"] == idempotency_key
                ]
                if matching_commands:
                    previous = max(
                        matching_commands,
                        key=lambda command: (
                            self._parse_time(command["created_at"]),
                            str(command["command_id"]),
                        ),
                    )
                    if previous["request_fingerprint"] == request_fingerprint:
                        selected_command = previous
                    elif not (
                        action == CommandAction.TASK_DISPATCH
                        and len(matching_commands) < 2
                        and previous["status"] == CommandStatus.FAILED.value
                        and isinstance(previous.get("error"), dict)
                        and previous["error"].get("code") == "connector_restarted"
                    ):
                        raise ConnectorError(
                            409,
                            "Idempotency key was already used with a different command",
                        )
            if selected_command is None:
                now = utc_now()
                command_id = new_id("cmd")
                selected_command = {
                    "command_id": command_id,
                    "binding_id": binding_id,
                    "device_id": binding["device_id"],
                    "runtime_id": binding["runtime_id"],
                    "agent_id": binding["agent_id"],
                    "binding_epoch": binding["binding_epoch"],
                    "session_id": payload.get("session_id")
                    or binding["last_session_id"],
                    "action": action.value,
                    "payload": payload,
                    "idempotency_key": idempotency_key or command_id,
                    "request_fingerprint": request_fingerprint,
                    "_session_generation": int(
                        binding.get("session_generation", 0)
                    ),
                    "status": CommandStatus.QUEUED.value,
                    "created_at": iso(now),
                    "expires_at": iso(now + timedelta(seconds=expires_in_seconds)),
                    "delivered_at": None,
                    "delivery_attempts": 0,
                    "updated_at": iso(now),
                    "result": None,
                    "error": None,
                    # A persistent adapter flips this only after its durable
                    # pre-delivery barrier has committed the record.
                    "_durable_ready": False,
                }
                self.commands[command_id] = selected_command
                self._append_audit(
                    "command.queued",
                    "platform",
                    {
                        "command_id": command_id,
                        "binding_id": binding_id,
                        "action": action.value,
                    },
                )
            command_id = str(selected_command["command_id"])
            delivery_device_id = str(selected_command["device_id"])

        # Durable implementations override this barrier. No command frame may
        # leave the process until its idempotency record is committed.
        await self._prepare_command_delivery(command_id)
        async with self._lock:
            current = self.commands.get(command_id)
            if current is None:
                raise ConnectorError(404, "Command not found")
            current["_durable_ready"] = True
        await self.deliver_pending(delivery_device_id)
        async with self._lock:
            return self._public_command(self.commands[command_id])

    @staticmethod
    def _arena_runtime_readiness_issues(
        runtime: dict[str, Any],
    ) -> list[str]:
        issues: list[str] = []
        if not bool(runtime.get("task_enabled")):
            issues.append("task_execution_disabled")
        if runtime.get("authentication_status") != "configured":
            issues.append("authentication_unavailable")
        if not bool(runtime.get("arena_compatible")):
            issues.append("arena_profile_unsupported")
        expected_isolation = {
            "codex": "read_only_ephemeral_schema",
            "claude-code": "no_tools_safe_mode_schema",
            "claude_code": "no_tools_safe_mode_schema",
        }.get(str(runtime.get("kind", "")))
        if (
            expected_isolation is None
            or runtime.get("arena_isolation") != expected_isolation
        ):
            issues.append("arena_isolation_unavailable")
        if not bool(runtime.get("local_execution_ready")):
            issues.append("runtime_readiness_unknown")
        return issues

    async def _prepare_command_delivery(self, command_id: str) -> None:
        """Persistence hook immediately before any command delivery attempt."""

        return None

    async def _prepare_outbound_sequence(
        self,
        device_id: str,
        sequence: int,
    ) -> None:
        """Persistence hook after reserving a sequence and before WSS delivery."""

        return None

    async def _commit_command_delivery(
        self,
        device_id: str,
        websocket: Any,
        generation: int,
        command: dict[str, Any],
    ) -> None:
        """Persistence hook after a Command frame is written to the socket."""

        return None

    async def _can_deliver_to_connection(
        self,
        device_id: str,
        websocket: Any,
        generation: int,
    ) -> bool:
        return True

    async def deliver_pending(self, device_id: str) -> int:
        """Deliver queued commands to the active socket without holding the state lock."""
        send_lock = self._send_locks.setdefault(device_id, asyncio.Lock())
        async with send_lock:
            return await self._deliver_pending_serialized(device_id)

    async def _deliver_pending_serialized(self, device_id: str) -> int:
        async with self._lock:
            device = self._get_device(device_id)
            self._ensure_not_revoked(device)
            websocket = self.connections.get(device_id)
            generation = int(device["_connection_generation"])
            ready = self.connection_ready_generations.get(device_id) == generation
            now = utc_now()
            queued: list[dict[str, Any]] = []
            for command in self.commands.values():
                if command["device_id"] != device_id or command["status"] != "queued":
                    continue
                if not command.get("_durable_ready", True):
                    continue
                if self._parse_time(command["expires_at"]) <= now:
                    command["status"] = CommandStatus.EXPIRED.value
                    command["updated_at"] = iso(now)
                    continue
                queued.append(command)
        if websocket is None or not ready:
            return 0
        if not await self._can_deliver_to_connection(
            device_id,
            websocket,
            generation,
        ):
            return 0

        delivered = 0
        for command in queued:
            if not await self._can_deliver_to_connection(
                device_id,
                websocket,
                generation,
            ):
                break
            async with self._lock:
                device = self._get_device(device_id)
                self._ensure_not_revoked(device)
                current = self.commands.get(command["command_id"])
                if (
                    self.connections.get(device_id) is not websocket
                    or device["_connection_generation"] != generation
                    or current is None
                    or current["status"] != CommandStatus.QUEUED.value
                ):
                    continue
                device["outbound_sequence"] += 1
                sequence = device["outbound_sequence"]
            await self._prepare_outbound_sequence(device_id, int(sequence))
            if not await self._can_deliver_to_connection(
                device_id,
                websocket,
                generation,
            ):
                break
            try:
                await websocket.send_json(
                    {
                        "type": "command",
                        "protocol_version": self.protocol_version,
                        "device_id": device_id,
                        "sequence": sequence,
                        "message_id": command["command_id"],
                        "sent_at": iso(utc_now()),
                        "payload": {
                            key: value
                            for key, value in command.items()
                            if key
                            not in {
                                "device_id",
                                "delivered_at",
                                "updated_at",
                                "request_fingerprint",
                                "_durable_ready",
                            }
                        },
                    }
                )
            except Exception:
                break
            async with self._lock:
                current = self.commands.get(command["command_id"])
                device = self._get_device(device_id)
                connection_is_current = (
                    self.connections.get(device_id) is websocket
                    and device["_connection_generation"] == generation
                )
                if (
                    connection_is_current
                    and current
                    and current["status"] == CommandStatus.QUEUED.value
                ):
                    current["status"] = CommandStatus.DELIVERED.value
                    current["delivered_at"] = iso(utc_now())
                    current["delivery_attempts"] += 1
                    current["updated_at"] = current["delivered_at"]
                    delivered += 1
                    delivered_command = dict(current)
                else:
                    delivered_command = None
            if delivered_command is not None:
                await self._commit_command_delivery(
                    device_id,
                    websocket,
                    generation,
                    delivered_command,
                )
        return delivered

    async def observe_inbound_sequence(
        self,
        device_id: str,
        sequence: Optional[int],
        expected_generation: Optional[int] = None,
    ) -> None:
        if sequence is None:
            return
        async with self._lock:
            device = self._get_device(device_id)
            self._ensure_not_revoked(device)
            self._ensure_connection_generation(device, expected_generation)
            previous = device.get("last_inbound_sequence")
            if previous is not None and sequence > previous + 1:
                self._append_audit(
                    "transport.sequence_gap",
                    device_id,
                    {
                        "device_id": device_id,
                        "expected": previous + 1,
                        "received": sequence,
                    },
                )
            if previous is None or sequence > previous:
                device["last_inbound_sequence"] = sequence

    async def resume_transport(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        """Reconcile durable WSS cursors before replaying queued commands."""

        required = {
            "last_gateway_sequence",
            "event_ack_through",
            "pending_result_ids",
        }
        if set(payload) != required:
            raise ConnectorError(422, "Invalid transport resume payload")
        last_gateway_sequence = payload["last_gateway_sequence"]
        event_ack_through = payload["event_ack_through"]
        pending_result_ids = payload["pending_result_ids"]
        if (
            not isinstance(last_gateway_sequence, int)
            or isinstance(last_gateway_sequence, bool)
            or not isinstance(event_ack_through, int)
            or isinstance(event_ack_through, bool)
            or last_gateway_sequence < 0
            or event_ack_through < 0
            or not isinstance(pending_result_ids, list)
            or len(pending_result_ids) > 512
            or any(
                not isinstance(item, str) or not item or len(item) > 256
                for item in pending_result_ids
            )
        ):
            raise ConnectorError(422, "Invalid transport resume payload")

        async with self._lock:
            device = self._get_device(device_id)
            self._ensure_not_revoked(device)
            self._ensure_connection_generation(device, expected_generation)
            gateway_sequence = int(device.get("outbound_sequence", 0))
            if last_gateway_sequence > gateway_sequence:
                raise ConnectorError(
                    409,
                    "Connector resume cursor is ahead of the Gateway",
                )
            authoritative_event_ack = self.event_ack_watermarks.get(device_id, 0)
            if event_ack_through > authoritative_event_ack:
                raise ConnectorError(
                    409,
                    "Connector event resume cursor is ahead of the Gateway",
                )
            accepted_result_ids = {
                str(record["result_id"])
                for record in self.agent_task_results.values()
                if record["device_id"] == device_id
                and self._result_ready_for_transport_ack(record)
            }
            now = utc_now()
            pending_command_ids = sorted(
                str(command["command_id"])
                for command in self.commands.values()
                if command["device_id"] == device_id
                and command["status"] == CommandStatus.QUEUED.value
                and self._parse_time(command["expires_at"]) > now
            )
            self._append_audit(
                "transport.resumed",
                device_id,
                {
                    "device_id": device_id,
                    "last_gateway_sequence": last_gateway_sequence,
                    "gateway_sequence": gateway_sequence,
                    "event_ack_through": authoritative_event_ack,
                    "pending_command_count": len(pending_command_ids),
                },
            )
            return {
                "accepted": True,
                "connection_generation": int(device["_connection_generation"]),
                "gateway_sequence": gateway_sequence,
                "event_ack_through": authoritative_event_ack,
                "accepted_result_ids": sorted(
                    accepted_result_ids.intersection(pending_result_ids)
                ),
                "pending_command_ids": pending_command_ids,
            }

    def _result_ready_for_transport_ack(self, record: dict[str, Any]) -> bool:
        """Whether resume may replace the normal result acknowledgement."""

        return True

    async def notify_task_available(
        self,
        binding_id: str,
        payload: dict[str, Any],
    ) -> bool:
        required = {
            "wake_id",
            "task_id",
            "binding_id",
            "binding_epoch",
            "deadline_at",
        }
        if set(payload) != required:
            raise ConnectorError(422, "Invalid task.available payload")
        if payload["binding_id"] != binding_id:
            raise ConnectorError(422, "task.available binding_id mismatch")
        async with self._lock:
            binding = self.bindings.get(binding_id)
            if binding is None:
                raise ConnectorError(404, "Binding not found")
            if int(payload["binding_epoch"]) != int(binding["binding_epoch"]):
                raise ConnectorError(409, "Stale Connector binding epoch")
            device_id = str(binding["device_id"])
            device = self._get_device(device_id)
            self._ensure_not_revoked(device)
            websocket = self.connections.get(device_id)
            generation = int(device["_connection_generation"])
            ready = self.connection_ready_generations.get(device_id) == generation
            send_lock = self._send_locks.setdefault(
                device_id,
                asyncio.Lock(),
            )
        if websocket is None or not ready:
            return False
        if not await self._can_deliver_to_connection(
            device_id,
            websocket,
            generation,
        ):
            return False
        async with send_lock:
            async with self._lock:
                device = self._get_device(device_id)
                if (
                    self.connections.get(device_id) is not websocket
                    or int(device["_connection_generation"]) != generation
                    or self.connection_ready_generations.get(device_id) != generation
                ):
                    return False
                device["outbound_sequence"] += 1
                sequence = int(device["outbound_sequence"])
            await self._prepare_outbound_sequence(device_id, sequence)
            if not await self._can_deliver_to_connection(
                device_id,
                websocket,
                generation,
            ):
                return False
            try:
                await websocket.send_json(
                    {
                        "type": "task.available",
                        "protocol_version": self.protocol_version,
                        "device_id": device_id,
                        "sequence": sequence,
                        "message_id": str(payload["wake_id"]),
                        "sent_at": iso(utc_now()),
                        "payload": dict(payload),
                    }
                )
            except Exception:
                return False
        async with self._lock:
            self._append_audit(
                "arena.task_available_sent",
                device_id,
                {
                    "wake_id": str(payload["wake_id"]),
                    "task_id": str(payload["task_id"]),
                    "binding_id": binding_id,
                    "binding_epoch": int(payload["binding_epoch"]),
                },
            )
        return True

    async def acknowledge_task_available(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        required = {"wake_id", "task_id", "binding_id", "binding_epoch"}
        if set(payload) != required:
            raise ConnectorError(
                422,
                "Invalid task.available acknowledgement",
            )
        async with self._lock:
            device = self._get_device(device_id)
            self._ensure_not_revoked(device)
            self._ensure_connection_generation(device, expected_generation)
            binding = self.bindings.get(str(payload["binding_id"]))
            if binding is None or binding["device_id"] != device_id:
                raise ConnectorError(403, "Wake binding does not belong to device")
            if int(payload["binding_epoch"]) != int(binding["binding_epoch"]):
                raise ConnectorError(409, "Stale Connector binding epoch")
            self._append_audit(
                "arena.task_available_received",
                device_id,
                {
                    "wake_id": str(payload["wake_id"]),
                    "task_id": str(payload["task_id"]),
                    "binding_id": str(payload["binding_id"]),
                    "binding_epoch": int(payload["binding_epoch"]),
                },
            )
            return {
                "accepted": True,
                "wake_id": str(payload["wake_id"]),
            }

    async def acknowledge_command(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        async with self._lock:
            device = self._get_device(device_id)
            self._ensure_not_revoked(device)
            self._ensure_connection_generation(device, expected_generation)
            command_id = str(payload.get("command_id", ""))
            command = self.commands.get(command_id)
            if not command or command["device_id"] != device_id:
                raise ConnectorError(404, "Command not found for this device")
            if (
                int(payload.get("binding_epoch", command["binding_epoch"]))
                != command["binding_epoch"]
            ):
                raise ConnectorError(409, "Stale binding epoch")
            try:
                status = CommandStatus(str(payload.get("status", "")))
            except ValueError as exc:
                raise ConnectorError(422, "Invalid command status") from exc
            allowed = {
                CommandStatus.ACCEPTED,
                CommandStatus.RUNNING,
                CommandStatus.SUCCEEDED,
                CommandStatus.FAILED,
                CommandStatus.REJECTED,
            }
            if status not in allowed:
                raise ConnectorError(422, "Connector cannot set that command status")
            current_status = CommandStatus(command["status"])
            transitions = {
                CommandStatus.QUEUED: allowed,
                CommandStatus.DELIVERED: allowed,
                CommandStatus.ACCEPTED: {
                    CommandStatus.ACCEPTED,
                    CommandStatus.RUNNING,
                    CommandStatus.SUCCEEDED,
                    CommandStatus.FAILED,
                    CommandStatus.REJECTED,
                },
                CommandStatus.RUNNING: {
                    CommandStatus.RUNNING,
                    CommandStatus.SUCCEEDED,
                    CommandStatus.FAILED,
                    CommandStatus.REJECTED,
                },
                CommandStatus.SUCCEEDED: {CommandStatus.SUCCEEDED},
                CommandStatus.FAILED: {CommandStatus.FAILED},
                CommandStatus.REJECTED: {CommandStatus.REJECTED},
                CommandStatus.EXPIRED: set(),
            }
            if status not in transitions.get(current_status, set()):
                raise ConnectorError(
                    409,
                    f"Invalid command transition {current_status.value} -> {status.value}",
                )
            command["status"] = status.value
            command["updated_at"] = iso(utc_now())
            command["result"] = payload.get("result")
            error_value = payload.get("error")
            if error_value is None and status in {
                CommandStatus.FAILED,
                CommandStatus.REJECTED,
            }:
                error_value = {
                    "code": payload.get("code", ""),
                    "message": payload.get("message", ""),
                }
            command["error"] = self._safe_error(error_value)

            binding = self.bindings.get(command["binding_id"])
            if binding:
                action = CommandAction(command["action"])
                lifecycle_is_older_than_restart = bool(
                    action
                    in {
                        CommandAction.SESSION_START,
                        CommandAction.SESSION_RESUME,
                    }
                    and int(command.get("_session_generation", 0))
                    < int(binding.get("session_generation", 0))
                )
                result = payload.get("result")
                result_session_id = (
                    result.get("session_id") if isinstance(result, dict) else None
                )
                result_task_id = (
                    result.get("task_id") if isinstance(result, dict) else None
                )
                session_id = payload.get("session_id") or result_session_id
                if session_id and not lifecycle_is_older_than_restart:
                    binding["last_session_id"] = str(session_id)[:128]
                if result_task_id:
                    binding["last_task_id"] = str(result_task_id)[:128]
                terminal_failure = status in {
                    CommandStatus.FAILED,
                    CommandStatus.REJECTED,
                }
                error_code = ""
                if isinstance(error_value, dict):
                    error_code = str(error_value.get("code", ""))
                session_was_lost = terminal_failure and error_code in {
                    "connector_restarted",
                    "session_not_found",
                    "stale_binding",
                }
                stopped_command_created_at = binding.get(
                    "last_stopped_command_created_at"
                )
                lifecycle_is_older_than_stop = bool(
                    stopped_command_created_at
                    and self._parse_time(command["created_at"])
                    <= self._parse_time(stopped_command_created_at)
                )

                if (
                    action == CommandAction.SESSION_STOP
                    and status == CommandStatus.SUCCEEDED
                ):
                    binding["status"] = BindingStatus.STOPPED.value
                    binding["last_stopped_command_created_at"] = command["created_at"]
                    binding["last_task_id"] = None
                elif (
                    action
                    in {
                        CommandAction.SESSION_START,
                        CommandAction.SESSION_RESUME,
                    }
                    and not lifecycle_is_older_than_stop
                    and not lifecycle_is_older_than_restart
                ):
                    if status in {
                        CommandStatus.ACCEPTED,
                        CommandStatus.RUNNING,
                        CommandStatus.SUCCEEDED,
                    }:
                        binding["status"] = BindingStatus.RUNNING.value
                    elif terminal_failure:
                        binding["status"] = BindingStatus.DEGRADED.value
                elif session_was_lost:
                    binding["status"] = BindingStatus.DEGRADED.value
                    binding["last_session_id"] = None
                    binding["last_task_id"] = None
                elif (
                    action == CommandAction.TASK_DISPATCH
                    and binding["status"] != BindingStatus.STOPPED.value
                    and status
                    in {
                        CommandStatus.ACCEPTED,
                        CommandStatus.RUNNING,
                        CommandStatus.SUCCEEDED,
                        CommandStatus.FAILED,
                        CommandStatus.REJECTED,
                    }
                ):
                    binding["status"] = BindingStatus.RUNNING.value
                elif (
                    action
                    in {
                        CommandAction.RUNTIME_PROBE,
                        CommandAction.SESSION_STOP,
                    }
                    and terminal_failure
                    and binding["status"] != BindingStatus.STOPPED.value
                ):
                    binding["status"] = BindingStatus.DEGRADED.value
                if (
                    action == CommandAction.TASK_DISPATCH
                    and status
                    in {
                        CommandStatus.SUCCEEDED,
                        CommandStatus.FAILED,
                        CommandStatus.REJECTED,
                    }
                    and result_task_id
                    and binding.get("last_task_id") == str(result_task_id)
                ):
                    binding["last_task_id"] = None
                binding["updated_at"] = command["updated_at"]
            self._append_audit(
                "command.acknowledged",
                device_id,
                {"command_id": command_id, "status": status.value},
            )
            return self._public_command(command)

    async def submit_agent_task_result(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        async with self._lock:
            device = self._get_device(device_id)
            self._ensure_not_revoked(device)
            self._ensure_connection_generation(device, expected_generation)
            binding_id = str(payload.get("binding_id", ""))
            binding = self.bindings.get(binding_id)
            if not binding or binding["device_id"] != device_id:
                raise ConnectorError(404, "Binding not found for this device")
            try:
                binding_epoch = int(payload.get("binding_epoch", 0))
            except (TypeError, ValueError) as exc:
                raise ConnectorError(
                    422, "binding_epoch must be a positive integer"
                ) from exc
            if binding_epoch != binding["binding_epoch"]:
                raise ConnectorError(409, "Stale binding epoch")
            try:
                result = AgentTaskResultV1.model_validate(payload.get("result"))
            except ValidationError as exc:
                raise ConnectorError(
                    422,
                    "result must be a valid arena.agent-result.v1 payload",
                ) from exc

            dispatched = next(
                (
                    command
                    for command in self.commands.values()
                    if command["binding_id"] == binding_id
                    and command["action"] == CommandAction.TASK_DISPATCH.value
                    and isinstance(command["payload"].get("task"), dict)
                    and command["payload"]["task"].get("taskId") == result.task_id
                ),
                None,
            )
            if dispatched is None:
                raise ConnectorError(
                    409,
                    "AgentTaskResult does not match a typed task dispatched to this binding",
                )

            result_payload = result.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            )
            result_hash = hashlib.sha256(
                json.dumps(
                    result_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            existing = self.agent_task_results.get(result.task_id)
            if existing is not None:
                if existing["result_hash"] != result_hash:
                    raise ConnectorError(
                        409,
                        "AgentTask already has a different terminal result",
                    )
                return self._public_agent_task_result_receipt(
                    existing,
                    disposition="replay",
                )

            received_at = iso(utc_now())
            record = {
                "task_id": result.task_id,
                "result_id": result.result_id,
                "binding_id": binding_id,
                "binding_epoch": binding_epoch,
                "device_id": device_id,
                "command_id": dispatched["command_id"],
                "result": result_payload,
                "result_hash": result_hash,
                "received_at": received_at,
            }
            self.agent_task_results[result.task_id] = record
            self._append_audit(
                "agent_task.result_received",
                device_id,
                {
                    "task_id": result.task_id,
                    "result_id": result.result_id,
                    "binding_id": binding_id,
                },
            )
            return self._public_agent_task_result_receipt(
                record,
                disposition="accepted",
            )

    async def append_runtime_event(
        self,
        device_id: str,
        payload: dict[str, Any],
        expected_generation: Optional[int] = None,
    ) -> dict[str, Any]:
        async with self._lock:
            device = self._get_device(device_id)
            self._ensure_not_revoked(device)
            self._ensure_connection_generation(device, expected_generation)
            binding_id = str(payload.get("binding_id", ""))
            binding = self.bindings.get(binding_id)
            if not binding or binding["device_id"] != device_id:
                raise ConnectorError(404, "Binding not found for this device")
            event_type = str(payload.get("event_type", ""))[:128]
            if not event_type:
                raise ConnectorError(422, "event_type is required")
            try:
                sequence = int(payload.get("sequence", 0))
            except (TypeError, ValueError) as exc:
                raise ConnectorError(
                    422, "runtime event sequence must be a positive integer"
                ) from exc
            if sequence <= 0:
                raise ConnectorError(
                    422, "runtime event sequence must be a positive integer"
                )
            supplied_event_id = str(payload.get("event_id", ""))[:128]
            pending = self.event_pending_sequences.setdefault(device_id, set())
            watermark = self.event_ack_watermarks.get(device_id, 0)
            for existing in self.events:
                same_source_id = (
                    bool(supplied_event_id)
                    and existing["source_event_id"] == supplied_event_id
                )
                same_sequence = existing["sequence"] == sequence
                if existing["device_id"] == device_id and (
                    same_source_id or same_sequence
                ):
                    duplicate = dict(existing)
                    duplicate["ack_through_sequence"] = self.event_ack_watermarks.get(
                        device_id, 0
                    )
                    return duplicate
            # Already acknowledged events may be older than the retained
            # observability window. Pending sequences can also age out while
            # waiting for a gap. Acknowledge either replay without appending a
            # duplicate database row.
            if sequence <= watermark or sequence in pending:
                return {
                    "duplicate": True,
                    "sequence": sequence,
                    "ack_through_sequence": watermark,
                }
            if sequence > watermark:
                pending.add(sequence)
            while watermark + 1 in pending:
                pending.remove(watermark + 1)
                watermark += 1
            self.event_ack_watermarks[device_id] = watermark
            device["event_ack_watermark"] = watermark
            device["event_pending_sequences"] = sorted(pending)
            event = {
                "event_id": new_id("event"),
                "source_event_id": supplied_event_id or None,
                "device_id": device_id,
                "binding_id": binding_id,
                "session_id": str(payload.get("session_id", ""))[:128] or None,
                "task_id": str(payload.get("task_id", ""))[:128] or None,
                "sequence": sequence,
                "event_type": event_type,
                "level": str(payload.get("level", "info"))[:32],
                "data": self._bounded_event_data(payload.get("data", {})),
                "occurred_at": str(payload.get("occurred_at") or iso(utc_now())),
                "received_at": iso(utc_now()),
            }
            self.events.append(event)
            if event_type in {
                "process.exited",
                "runtime.task.completed",
                "task.completed",
            }:
                event_task_id = event.get("task_id")
                if event_task_id and binding.get("last_task_id") == event_task_id:
                    binding["last_task_id"] = None
                    binding["updated_at"] = event["received_at"]
            if len(self.events) > 10_000:
                del self.events[:1_000]
            response = dict(event)
            response["ack_through_sequence"] = watermark
            return response

    async def list_events(
        self, binding_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        async with self._lock:
            if binding_id not in self.bindings:
                raise ConnectorError(404, "Binding not found")
            values = [
                dict(event)
                for event in self.events
                if event["binding_id"] == binding_id
            ]
            return values[-max(1, min(limit, 1000)) :]

    async def list_commands(
        self, binding_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self._lock:
            if binding_id not in self.bindings:
                raise ConnectorError(404, "Binding not found")
            values = [
                self._public_command(command)
                for command in self.commands.values()
                if command["binding_id"] == binding_id
            ]
            return values[-max(1, min(limit, 500)) :]

    async def list_audit(
        self, limit: int = 200, owner_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        async with self._lock:
            values = [
                dict(item)
                for item in self.audit
                if owner_id is None or item.get("owner_id") == owner_id
            ]
            return values[-max(1, min(limit, 1000)) :]

    def _validate_command_payload(
        self, action: CommandAction, payload: dict[str, Any]
    ) -> None:
        if not isinstance(payload, dict):
            raise ConnectorError(422, "Command payload must be an object")
        keys = set(payload)
        forbidden = keys & self._forbidden_payload_fields
        if forbidden:
            raise ConnectorError(
                422,
                f"Arbitrary execution fields are forbidden: {', '.join(sorted(forbidden))}",
            )
        unknown = keys - self._payload_fields[action]
        if unknown:
            raise ConnectorError(
                422,
                f"Unsupported payload fields for {action.value}: {', '.join(sorted(unknown))}",
            )
        missing = self._required_payload_fields[action] - keys
        if missing:
            raise ConnectorError(
                422,
                f"Missing payload fields for {action.value}: {', '.join(sorted(missing))}",
            )
        if action == CommandAction.TASK_DISPATCH:
            has_prompt = "prompt" in payload
            has_task = "task" in payload
            if has_prompt == has_task:
                raise ConnectorError(
                    422,
                    "task.dispatch requires exactly one of prompt or task",
                )
            if has_prompt and "request_id" not in payload:
                raise ConnectorError(
                    422,
                    "Missing payload fields for task.dispatch: request_id",
                )
            if has_task:
                try:
                    ArenaAgentTaskV1.model_validate(payload["task"])
                except ValidationError as exc:
                    raise ConnectorError(
                        422,
                        "task must be a valid arena.agent-task.v1 payload",
                    ) from exc
        working_directory = payload.get("working_directory")
        if working_directory is not None and (
            not isinstance(working_directory, str)
            or not working_directory.strip()
            or len(working_directory) > 2048
        ):
            raise ConnectorError(
                422,
                "working_directory must be a non-empty string no longer than 2048 characters",
            )
        prompt = payload.get("prompt") or payload.get("initial_prompt")
        if prompt is not None and (
            not isinstance(prompt, str) or len(prompt) > 100_000
        ):
            raise ConnectorError(
                422, "Prompt must be a string no longer than 100000 characters"
            )
        session_id = payload.get("session_id")
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id or len(session_id) > 128
        ):
            raise ConnectorError(
                422,
                "session_id must be a non-empty string no longer than 128 characters",
            )
        environment_refs = payload.get("environment_refs")
        if environment_refs is not None and (
            not isinstance(environment_refs, list)
            or len(environment_refs) > 64
            or any(
                not isinstance(value, str) or len(value) > 256
                for value in environment_refs
            )
        ):
            raise ConnectorError(
                422, "environment_refs must be a bounded list of secret references"
            )

    @staticmethod
    def _public_agent_task_result_receipt(
        record: dict[str, Any],
        *,
        disposition: str,
    ) -> dict[str, Any]:
        return {
            "task_id": record["task_id"],
            "result_id": record["result_id"],
            "binding_id": record["binding_id"],
            "disposition": disposition,
            "received_at": record["received_at"],
        }

    def _public_pairing(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if key not in {"device_code_hash"}
        }

    def _public_device(self, device: dict[str, Any]) -> dict[str, Any]:
        value = {
            key: item
            for key, item in device.items()
            if key not in {"token_hash", "_connection_generation"}
        }
        value["status"] = self._effective_device_status(device)
        return value

    @staticmethod
    def _public_command(command: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in command.items()
            if key != "request_fingerprint" and not key.startswith("_")
        }

    def _effective_device_status(self, device: dict[str, Any]) -> str:
        if device.get("revoked_at"):
            return "revoked"
        if device["device_id"] not in self.connections:
            return "offline"
        if not device.get("last_seen_at"):
            return "offline"
        age = utc_now() - self._parse_time(device["last_seen_at"])
        return (
            "online"
            if age.total_seconds() <= self.heartbeat_lease_seconds
            else "offline"
        )

    def _get_pairing(self, user_code: str) -> dict[str, Any]:
        normalized = user_code.strip().upper()
        record = self.pairings.get(normalized)
        if not record:
            raise ConnectorError(404, "Pairing code not found")
        return record

    def _get_device(self, device_id: str) -> dict[str, Any]:
        device = self.devices.get(device_id)
        if not device:
            raise ConnectorError(404, "Device not found")
        return device

    @staticmethod
    def _ensure_not_revoked(device: dict[str, Any]) -> None:
        if device.get("revoked_at"):
            raise ConnectorError(410, "Device has been revoked")

    @staticmethod
    def _ensure_connection_generation(
        device: dict[str, Any], expected_generation: Optional[int]
    ) -> None:
        if (
            expected_generation is not None
            and device["_connection_generation"] != expected_generation
        ):
            raise ConnectorError(
                409, "WebSocket is no longer an active device connection"
            )

    def _refresh_pairing_expiry(self, record: dict[str, Any]) -> None:
        if (
            record["status"]
            in {PairingStatus.PENDING.value, PairingStatus.APPROVED.value}
            and self._parse_time(record["expires_at"]) <= utc_now()
        ):
            record["status"] = PairingStatus.EXPIRED.value

    def _unique_user_code(self) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        while True:
            value = "".join(secrets.choice(alphabet) for _ in range(8))
            code = f"{value[:4]}-{value[4:]}"
            if code not in self.pairings:
                return code

    def _append_audit(self, action: str, actor: str, metadata: dict[str, Any]) -> None:
        owner_id = self._resolve_audit_owner(actor, metadata)
        self.audit.append(
            {
                "audit_id": new_id("audit"),
                "owner_id": owner_id,
                "action": action,
                "actor": actor,
                "metadata": metadata,
                "occurred_at": iso(utc_now()),
            }
        )
        if len(self.audit) > 10_000:
            del self.audit[:1_000]

    def _resolve_audit_owner(
        self, actor: str, metadata: dict[str, Any]
    ) -> Optional[str]:
        device_id = metadata.get("device_id")
        device = self.devices.get(str(device_id)) if device_id else None
        if device:
            return str(device["owner_id"])
        binding_id = metadata.get("binding_id")
        binding = self.bindings.get(str(binding_id)) if binding_id else None
        if binding:
            binding_device = self.devices.get(str(binding["device_id"]))
            if binding_device:
                return str(binding_device["owner_id"])
        command_id = metadata.get("command_id")
        command = self.commands.get(str(command_id)) if command_id else None
        if command:
            command_device = self.devices.get(str(command["device_id"]))
            if command_device:
                return str(command_device["owner_id"])
        pairing_id = metadata.get("pairing_id")
        if pairing_id:
            pairing = next(
                (
                    value
                    for value in self.pairings.values()
                    if value["pairing_id"] == pairing_id
                ),
                None,
            )
            if pairing and pairing.get("owner_id"):
                return str(pairing["owner_id"])
        known_owners = {
            str(value["owner_id"])
            for value in self.devices.values()
            if value.get("owner_id")
        }
        return actor if actor in known_owners else None

    @staticmethod
    def _safe_error(value: Any) -> Optional[dict[str, Any]]:
        if value is None:
            return None
        if isinstance(value, str):
            return {"message": ConnectorGateway._redact_text(value[:2048])}
        if isinstance(value, dict):
            return {
                str(key)[:128]: (
                    "[REDACTED]"
                    if ConnectorGateway._sensitive_key_pattern.search(str(key))
                    else ConnectorGateway._redact_text(str(item)[:2048])
                )
                for key, item in list(value.items())[:20]
            }
        return {"message": ConnectorGateway._redact_text(str(value)[:2048])}

    @staticmethod
    def _bounded_event_data(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {"value": ConnectorGateway._bounded_event_value(value, "", 0)}
        return {
            str(key)[:128]: ConnectorGateway._bounded_event_value(item, str(key), 0)
            for key, item in list(value.items())[:100]
        }

    @staticmethod
    def _bounded_event_value(value: Any, key: str, depth: int) -> Any:
        if ConnectorGateway._sensitive_key_pattern.search(key):
            return "[REDACTED]"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return ConnectorGateway._redact_text(value[:8192])
        if depth >= 4:
            return ConnectorGateway._redact_text(str(value)[:8192])
        if isinstance(value, dict):
            return {
                str(child_key)[:128]: ConnectorGateway._bounded_event_value(
                    child_value, str(child_key), depth + 1
                )
                for child_key, child_value in list(value.items())[:100]
            }
        if isinstance(value, (list, tuple)):
            return [
                ConnectorGateway._bounded_event_value(item, key, depth + 1)
                for item in list(value)[:100]
            ]
        return ConnectorGateway._redact_text(str(value)[:8192])

    @staticmethod
    def _redact_text(value: str) -> str:
        redacted = value
        for pattern in ConnectorGateway._sensitive_text_patterns:
            redacted = pattern.sub(
                lambda match: (
                    (match.group(1) if match.lastindex else "") + "[REDACTED]"
                ),
                redacted,
            )
        return redacted

    @staticmethod
    def _parse_time(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _model_dump(model: Any) -> dict[str, Any]:
        if hasattr(model, "model_dump"):
            return model.model_dump()
        return model.dict()
