"""Wire models shared by the Connector Gateway REST and WebSocket APIs."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class PairingStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    CONSUMED = "consumed"
    EXPIRED = "expired"


class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"


class BindingStatus(str, Enum):
    AVAILABLE = "available"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPED = "stopped"


class CommandAction(str, Enum):
    """The complete MVP control surface.

    Deliberately absent: arbitrary shell commands, executable paths and argv.
    """

    RUNTIME_PROBE = "runtime.probe"
    SESSION_START = "session.start"
    TASK_DISPATCH = "task.dispatch"
    TASK_CANCEL = "task.cancel"
    SESSION_STOP = "session.stop"
    SESSION_RESUME = "session.resume"


class CommandStatus(str, Enum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class CreatePairingRequest(BaseModel):
    # This is only a UI hint. Ownership is established by the authenticated
    # user who approves the code, never by the unauthenticated Connector.
    owner_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    device_name: str = Field(default="Local computer", min_length=1, max_length=128)


class AcceptInviteRequest(BaseModel):
    invite_code: str = Field(min_length=20, max_length=512)
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=12, max_length=1024)


class RegisterRequest(BaseModel):
    invite_code: str = Field(min_length=20, max_length=512)
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=12, max_length=1024)


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class ApprovePairingRequest(BaseModel):
    owner_id: str = Field(default="demo-user", min_length=1, max_length=128)


class ExchangePairingRequest(BaseModel):
    device_code: str = Field(min_length=16, max_length=512)


class RevokeDeviceRequest(BaseModel):
    owner_id: str = Field(default="demo-user", min_length=1, max_length=128)


class CreateBindingRequest(BaseModel):
    runtime_id: str = Field(min_length=1, max_length=128)
    agent_id: Optional[str] = Field(default=None, max_length=128)
    display_name: Optional[str] = Field(default=None, max_length=128)


class CreateCommandRequest(BaseModel):
    action: CommandAction
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)
    expires_in_seconds: int = Field(default=300, ge=5, le=3600)


class RuntimeInventoryItem(BaseModel):
    runtime_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    executable_path: str = Field(min_length=1, max_length=2048)
    version: Optional[str] = Field(default=None, max_length=256)
    available: bool = True
    capabilities: list[str] = Field(default_factory=list)
    auth_modes: list[str] = Field(default_factory=list)
    detected_at: Optional[str] = None


class ConnectorEnvelope(BaseModel):
    type: str = Field(min_length=1, max_length=64)
    protocol_version: Optional[str] = Field(default=None, max_length=16)
    message_id: Optional[str] = Field(default=None, max_length=128)
    device_id: Optional[str] = Field(default=None, max_length=128)
    sequence: Optional[int] = Field(default=None, ge=0)
    sent_at: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
