"""Repository contracts and a deterministic in-memory test implementation."""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol


class DuplicateIdentityError(Exception):
    pass


class InvalidInviteError(Exception):
    pass


class ConnectionFenceError(Exception):
    pass


class ConnectorRepository(Protocol):
    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def seed_invite(
        self, token_hash: str, expires_at: datetime | None = None
    ) -> None: ...

    async def is_invite_available(
        self, token_hash: str, checked_at: datetime
    ) -> bool: ...

    async def consume_invite_and_create_user(
        self,
        token_hash: str,
        user_id: str,
        username: str,
        password_hash: str | None,
        temporary: bool,
        created_at: datetime,
    ) -> dict[str, Any]: ...

    async def create_password_user(
        self,
        user_id: str,
        username: str,
        password_hash: str,
        created_at: datetime,
    ) -> dict[str, Any]: ...

    async def get_user_by_username(
        self, normalized_username: str
    ) -> dict[str, Any] | None: ...

    async def get_or_create_oauth_user(
        self,
        provider: str,
        subject: str,
        preferred_username: str,
        created_at: datetime,
    ) -> tuple[dict[str, Any], bool]: ...

    async def create_session(
        self,
        token_hash: str,
        user_id: str,
        csrf_hash: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> None: ...

    async def get_session(self, token_hash: str) -> dict[str, Any] | None: ...

    async def revoke_session(self, token_hash: str, revoked_at: datetime) -> None: ...

    async def load_gateway_state(self) -> dict[str, Any]: ...

    async def save_gateway_state(self, state: dict[str, Any]) -> None: ...

    async def claim_device_connection(
        self,
        device_id: str,
        instance_id: str,
        lease_seconds: int,
    ) -> int: ...

    async def is_device_connection_owner(
        self,
        device_id: str,
        instance_id: str,
        fencing_token: int,
    ) -> bool: ...

    async def has_active_device_connection(self, device_id: str) -> bool: ...

    async def renew_device_connection(
        self,
        device_id: str,
        instance_id: str,
        fencing_token: int,
        lease_seconds: int,
    ) -> bool: ...

    async def release_device_connection(
        self,
        device_id: str,
        instance_id: str,
        fencing_token: int,
    ) -> bool: ...

    async def release_device_connection_and_save_device(
        self,
        device_id: str,
        instance_id: str,
        fencing_token: int,
        device: dict[str, Any],
    ) -> bool: ...

    async def list_queued_command_routes_for_connection_owner(
        self,
        instance_id: str,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    async def get_command_by_idempotency_key(
        self,
        binding_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None: ...

    async def get_binding(self, binding_id: str) -> dict[str, Any] | None: ...

    async def save_command_for_connection_owner(
        self,
        device_id: str,
        instance_id: str,
        fencing_token: int,
        command: dict[str, Any],
    ) -> bool: ...

    async def save_outbound_sequence_for_connection_owner(
        self,
        device_id: str,
        instance_id: str,
        fencing_token: int,
        sequence: int,
    ) -> bool: ...

    async def get_device(self, device_id: str) -> dict[str, Any] | None: ...

    async def load_device_runtime_state(self, device_id: str) -> dict[str, Any]: ...

    async def list_devices(self, owner_id: str | None = None) -> list[dict[str, Any]]: ...

    async def list_bindings(self, device_id: str | None = None) -> list[dict[str, Any]]: ...

    async def list_commands(
        self,
        binding_id: str,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    async def list_events(
        self,
        binding_id: str,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    async def list_audit(
        self,
        limit: int,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]: ...


class MemoryConnectorRepository:
    """In-memory repository used only by contract tests and local unit tests."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.invites: dict[str, dict[str, Any]] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.users_by_name: dict[str, str] = {}
        self.oauth_users: dict[tuple[str, str], str] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.gateway_state: dict[str, Any] = {
            "pairings": [],
            "devices": [],
            "bindings": [],
            "commands": [],
            "agent_task_results": [],
            "events": [],
            "audit": [],
        }
        self.connection_leases: dict[str, dict[str, Any]] = {}

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def seed_invite(
        self, token_hash: str, expires_at: datetime | None = None
    ) -> None:
        async with self._lock:
            self.invites.setdefault(
                token_hash,
                {
                    "token_hash": token_hash,
                    "expires_at": expires_at,
                    "consumed_at": None,
                    "consumed_by": None,
                },
            )

    async def is_invite_available(self, token_hash: str, checked_at: datetime) -> bool:
        async with self._lock:
            invite = self.invites.get(token_hash)
            return bool(
                invite
                and invite["consumed_at"] is None
                and (invite["expires_at"] is None or invite["expires_at"] > checked_at)
            )

    async def consume_invite_and_create_user(
        self,
        token_hash: str,
        user_id: str,
        username: str,
        password_hash: str | None,
        temporary: bool,
        created_at: datetime,
    ) -> dict[str, Any]:
        async with self._lock:
            invite = self.invites.get(token_hash)
            if (
                not invite
                or invite["consumed_at"] is not None
                or (
                    invite["expires_at"] is not None
                    and invite["expires_at"] <= created_at
                )
            ):
                raise InvalidInviteError
            if username in self.users_by_name:
                raise DuplicateIdentityError
            user = {
                "user_id": user_id,
                "username": username,
                "password_hash": password_hash,
                "temporary": temporary,
                "identity_provider": "password",
                "provider_subject": None,
                "created_at": created_at,
                "disabled_at": None,
            }
            self.users[user_id] = user
            self.users_by_name[username] = user_id
            invite["consumed_at"] = created_at
            invite["consumed_by"] = user_id
            return copy.deepcopy(user)

    async def create_password_user(
        self,
        user_id: str,
        username: str,
        password_hash: str,
        created_at: datetime,
    ) -> dict[str, Any]:
        async with self._lock:
            if username in self.users_by_name:
                raise DuplicateIdentityError
            user = {
                "user_id": user_id,
                "username": username,
                "password_hash": password_hash,
                "temporary": False,
                "identity_provider": "password",
                "provider_subject": None,
                "created_at": created_at,
                "disabled_at": None,
            }
            self.users[user_id] = user
            self.users_by_name[username] = user_id
            return copy.deepcopy(user)

    async def get_user_by_username(
        self, normalized_username: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            user_id = self.users_by_name.get(normalized_username)
            return copy.deepcopy(self.users.get(user_id)) if user_id else None

    async def get_or_create_oauth_user(
        self,
        provider: str,
        subject: str,
        preferred_username: str,
        created_at: datetime,
    ) -> tuple[dict[str, Any], bool]:
        async with self._lock:
            identity_key = (provider, subject)
            existing_id = self.oauth_users.get(identity_key)
            if existing_id:
                return copy.deepcopy(self.users[existing_id]), False

            username = preferred_username
            if username in self.users_by_name:
                username = f"{provider}-{subject}"[:64]
            if username in self.users_by_name:
                raise DuplicateIdentityError

            user_id = f"user_{uuid.uuid4().hex[:20]}"
            user = {
                "user_id": user_id,
                "username": username,
                "password_hash": None,
                "temporary": False,
                "identity_provider": provider,
                "provider_subject": subject,
                "created_at": created_at,
                "disabled_at": None,
            }
            self.users[user_id] = user
            self.users_by_name[username] = user_id
            self.oauth_users[identity_key] = user_id
            return copy.deepcopy(user), True

    async def create_session(
        self,
        token_hash: str,
        user_id: str,
        csrf_hash: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        async with self._lock:
            self.sessions[token_hash] = {
                "token_hash": token_hash,
                "user_id": user_id,
                "csrf_hash": csrf_hash,
                "created_at": created_at,
                "expires_at": expires_at,
                "revoked_at": None,
            }

    async def get_session(self, token_hash: str) -> dict[str, Any] | None:
        async with self._lock:
            session = self.sessions.get(token_hash)
            if not session:
                return None
            user = self.users.get(session["user_id"])
            if not user:
                return None
            return copy.deepcopy({**session, **user})

    async def revoke_session(self, token_hash: str, revoked_at: datetime) -> None:
        async with self._lock:
            if token_hash in self.sessions:
                self.sessions[token_hash]["revoked_at"] = revoked_at

    async def load_gateway_state(self) -> dict[str, Any]:
        async with self._lock:
            return copy.deepcopy(self.gateway_state)

    async def save_gateway_state(self, state: dict[str, Any]) -> None:
        async with self._lock:
            fence = state.get("_connection_fence")
            if fence is not None:
                device_id = str(fence["device_id"])
                lease = self.connection_leases.get(device_id)
                current_device = next(
                    (
                        item
                        for item in self.gateway_state.get("devices", [])
                        if str(item["device_id"]) == device_id
                    ),
                    None,
                )
                if not (
                    lease
                    and lease["instance_id"] == str(fence["instance_id"])
                    and int(lease["fencing_token"])
                    == int(fence["fencing_token"])
                    and lease["lease_expires_at"] > datetime.now(timezone.utc)
                    and current_device is not None
                    and not current_device.get("revoked_at")
                ):
                    raise ConnectionFenceError
            # Mutable entities are snapshots. Events and audit entries are
            # append-only deltas so production heartbeats do not replay the
            # complete observability history.
            current_events = {
                str(item["event_id"]): item
                for item in self.gateway_state.get("events", [])
            }
            for item in state.get("events", []):
                current_events[str(item["event_id"])] = copy.deepcopy(item)
            current_audit = {
                str(item["audit_id"]): item
                for item in self.gateway_state.get("audit", [])
            }
            for item in state.get("audit", []):
                current_audit[str(item["audit_id"])] = copy.deepcopy(item)

            key_fields = {
                "pairings": "pairing_id",
                "devices": "device_id",
                "bindings": "binding_id",
                "commands": "command_id",
                "agent_task_results": "task_id",
            }
            replace_collections = set(state.get("_replace_collections", []))
            mutable: dict[str, list[dict[str, Any]]] = {}
            for collection, key_field in key_fields.items():
                incoming = copy.deepcopy(state.get(collection, []))
                if (
                    not state.get("_incremental", False)
                    or collection in replace_collections
                ):
                    mutable[collection] = incoming
                    continue
                merged = {
                    str(item[key_field]): copy.deepcopy(item)
                    for item in self.gateway_state.get(collection, [])
                }
                for item in incoming:
                    merged[str(item[key_field])] = item
                mutable[collection] = list(merged.values())

            self.gateway_state = {
                **mutable,
                "events": list(current_events.values())[-10_000:],
                "audit": list(current_audit.values())[-10_000:],
            }

    async def claim_device_connection(
        self,
        device_id: str,
        instance_id: str,
        lease_seconds: int,
    ) -> int:
        async with self._lock:
            previous = self.connection_leases.get(device_id)
            fencing_token = int(previous["fencing_token"]) + 1 if previous else 1
            now = datetime.now(timezone.utc)
            self.connection_leases[device_id] = {
                "device_id": device_id,
                "instance_id": instance_id,
                "fencing_token": fencing_token,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
            }
            for command in self.gateway_state.get("commands", []):
                if (
                    command["device_id"] == device_id
                    and command["status"] == "delivered"
                ):
                    command["status"] = "queued"
                    command["delivered_at"] = None
                    command["updated_at"] = now.isoformat().replace("+00:00", "Z")
            return fencing_token

    async def is_device_connection_owner(
        self,
        device_id: str,
        instance_id: str,
        fencing_token: int,
    ) -> bool:
        async with self._lock:
            lease = self.connection_leases.get(device_id)
            return bool(
                lease
                and lease["instance_id"] == instance_id
                and int(lease["fencing_token"]) == fencing_token
                and lease["lease_expires_at"] > datetime.now(timezone.utc)
            )

    async def has_active_device_connection(self, device_id: str) -> bool:
        async with self._lock:
            lease = self.connection_leases.get(device_id)
            return bool(
                lease
                and lease["lease_expires_at"] > datetime.now(timezone.utc)
            )

    async def renew_device_connection(
        self,
        device_id: str,
        instance_id: str,
        fencing_token: int,
        lease_seconds: int,
    ) -> bool:
        async with self._lock:
            lease = self.connection_leases.get(device_id)
            now = datetime.now(timezone.utc)
            if not (
                lease
                and lease["instance_id"] == instance_id
                and int(lease["fencing_token"]) == fencing_token
                and lease["lease_expires_at"] > now
            ):
                return False
            lease["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
            return True

    async def release_device_connection(
        self,
        device_id: str,
        instance_id: str,
        fencing_token: int,
    ) -> bool:
        async with self._lock:
            lease = self.connection_leases.get(device_id)
            if not (
                lease
                and lease["instance_id"] == instance_id
                and int(lease["fencing_token"]) == fencing_token
            ):
                return False
            # Retain the row/token so a future claim cannot reuse a stale token.
            lease["lease_expires_at"] = datetime.now(timezone.utc)
            return True

    async def release_device_connection_and_save_device(
        self,
        device_id: str,
        instance_id: str,
        fencing_token: int,
        device: dict[str, Any],
    ) -> bool:
        async with self._lock:
            lease = self.connection_leases.get(device_id)
            if not (
                lease
                and lease["instance_id"] == instance_id
                and int(lease["fencing_token"]) == fencing_token
            ):
                return False
            lease["lease_expires_at"] = datetime.now(timezone.utc)
            devices = {
                str(item["device_id"]): copy.deepcopy(item)
                for item in self.gateway_state.get("devices", [])
            }
            current = devices.get(device_id)
            # A control-plane revoke is monotonic. A WSS worker can still be
            # unwinding an old socket, but its offline projection must not
            # restore the old token or erase revoked_at.
            if current is None or not current.get("revoked_at"):
                devices[device_id] = copy.deepcopy(device)
            self.gateway_state["devices"] = list(devices.values())
            return True

    async def list_queued_command_routes_for_connection_owner(
        self,
        instance_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        async with self._lock:
            owned_device_ids = {
                device_id
                for device_id, lease in self.connection_leases.items()
                if lease["instance_id"] == instance_id
                and lease["lease_expires_at"] > now
            }
            revoked_device_ids = {
                str(device["device_id"])
                for device in self.gateway_state.get("devices", [])
                if device.get("revoked_at")
            }
            owned_device_ids.difference_update(revoked_device_ids)
            bindings = {
                str(binding["binding_id"]): binding
                for binding in self.gateway_state.get("bindings", [])
            }
            routes = [
                {
                    "command": copy.deepcopy(command),
                    "binding": copy.deepcopy(bindings[str(command["binding_id"])]),
                }
                for command in self.gateway_state.get("commands", [])
                if command["device_id"] in owned_device_ids
                and command["status"] == "queued"
                and str(command["binding_id"]) in bindings
                and datetime.fromisoformat(
                    str(command["expires_at"]).replace("Z", "+00:00")
                )
                > now
            ]
            routes.sort(
                key=lambda route: (
                    str(route["command"]["created_at"]),
                    str(route["command"]["command_id"]),
                )
            )
            return routes[:limit]

    async def get_command_by_idempotency_key(
        self,
        binding_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        async with self._lock:
            command = next(
                (
                    item
                    for item in self.gateway_state.get("commands", [])
                    if item["binding_id"] == binding_id
                    and item["idempotency_key"] == idempotency_key
                ),
                None,
            )
            return copy.deepcopy(command) if command is not None else None

    async def get_binding(self, binding_id: str) -> dict[str, Any] | None:
        async with self._lock:
            binding = next(
                (
                    item
                    for item in self.gateway_state.get("bindings", [])
                    if str(item["binding_id"]) == binding_id
                ),
                None,
            )
            return copy.deepcopy(binding) if binding is not None else None

    async def save_command_for_connection_owner(
        self,
        device_id: str,
        instance_id: str,
        fencing_token: int,
        command: dict[str, Any],
    ) -> bool:
        async with self._lock:
            lease = self.connection_leases.get(device_id)
            if not (
                lease
                and lease["instance_id"] == instance_id
                and int(lease["fencing_token"]) == fencing_token
                and lease["lease_expires_at"] > datetime.now(timezone.utc)
            ):
                return False
            commands = {
                str(item["command_id"]): copy.deepcopy(item)
                for item in self.gateway_state.get("commands", [])
            }
            current = commands.get(str(command["command_id"]))
            if current is None or current.get("status") != "queued":
                return False
            commands[str(command["command_id"])] = copy.deepcopy(command)
            self.gateway_state["commands"] = list(commands.values())
            return True

    async def save_outbound_sequence_for_connection_owner(
        self,
        device_id: str,
        instance_id: str,
        fencing_token: int,
        sequence: int,
    ) -> bool:
        async with self._lock:
            lease = self.connection_leases.get(device_id)
            if not (
                lease
                and lease["instance_id"] == instance_id
                and int(lease["fencing_token"]) == fencing_token
                and lease["lease_expires_at"] > datetime.now(timezone.utc)
            ):
                return False
            for device in self.gateway_state.get("devices", []):
                if str(device["device_id"]) == device_id:
                    device["outbound_sequence"] = sequence
                    return True
            return False

    async def get_device(self, device_id: str) -> dict[str, Any] | None:
        async with self._lock:
            device = next(
                (
                    item
                    for item in self.gateway_state.get("devices", [])
                    if str(item["device_id"]) == device_id
                ),
                None,
            )
            return copy.deepcopy(device) if device is not None else None

    async def load_device_runtime_state(self, device_id: str) -> dict[str, Any]:
        async with self._lock:
            device = next(
                (
                    item
                    for item in self.gateway_state.get("devices", [])
                    if str(item["device_id"]) == device_id
                ),
                None,
            )
            return {
                "device": copy.deepcopy(device),
                "bindings": [
                    copy.deepcopy(item)
                    for item in self.gateway_state.get("bindings", [])
                    if str(item["device_id"]) == device_id
                ],
                "commands": [
                    copy.deepcopy(item)
                    for item in self.gateway_state.get("commands", [])
                    if str(item["device_id"]) == device_id
                ],
                "agent_task_results": [
                    copy.deepcopy(item)
                    for item in self.gateway_state.get("agent_task_results", [])
                    if str(item["device_id"]) == device_id
                ],
                "events": [
                    copy.deepcopy(item)
                    for item in self.gateway_state.get("events", [])
                    if str(item["device_id"]) == device_id
                ],
            }

    async def list_devices(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            now = datetime.now(timezone.utc)
            return [
                {
                    **copy.deepcopy(item),
                    "_has_active_connection": bool(
                        (lease := self.connection_leases.get(str(item["device_id"])))
                        and lease["lease_expires_at"] > now
                    ),
                }
                for item in self.gateway_state.get("devices", [])
                if owner_id is None or str(item["owner_id"]) == owner_id
            ]

    async def list_bindings(
        self,
        device_id: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                copy.deepcopy(item)
                for item in self.gateway_state.get("bindings", [])
                if device_id is None or str(item["device_id"]) == device_id
            ]

    async def list_commands(
        self,
        binding_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                copy.deepcopy(item)
                for item in self.gateway_state.get("commands", [])
                if str(item["binding_id"]) == binding_id
            ][-limit:]

    async def list_events(
        self,
        binding_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                copy.deepcopy(item)
                for item in self.gateway_state.get("events", [])
                if str(item["binding_id"]) == binding_id
            ][-limit:]

    async def list_audit(
        self,
        limit: int,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                copy.deepcopy(item)
                for item in self.gateway_state.get("audit", [])
                if owner_id is None or item.get("owner_id") == owner_id
            ][-limit:]
