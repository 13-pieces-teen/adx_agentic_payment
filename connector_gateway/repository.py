"""Repository contracts and a deterministic in-memory test implementation."""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import datetime
from typing import Any, Protocol


class DuplicateIdentityError(Exception):
    pass


class InvalidInviteError(Exception):
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
