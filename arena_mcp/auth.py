"""Short-lived, binding-scoped authorization for the Arena MCP data plane."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable, Iterable
from typing import Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)
from typing_extensions import Annotated


MCP_AUDIENCE: Final = "arena402-mcp"
MCP_TOKEN_VERSION: Final = "arena-mcp-token.v1"
MCP_SCOPES: Final[frozenset[str]] = frozenset(
    {
        "task:claim",
        "task:read",
        "task:submit",
        "task:release",
    }
)

Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class ExecutionTokenError(ValueError):
    """Raised when an MCP execution token cannot be trusted."""


class ExecutionTokenClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal["arena-mcp-token.v1"]
    issuer: Literal["arena402"]
    subject: Identifier
    audience: Literal["arena402-mcp"]
    device_id: Identifier
    binding_id: Identifier
    binding_epoch: int = Field(gt=0)
    scopes: tuple[str, ...]
    issued_at: int = Field(ge=0)
    expires_at: int = Field(gt=0)
    token_id: Identifier

    @field_validator("scopes", mode="before")
    @classmethod
    def parse_scopes(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @property
    def worker_id(self) -> str:
        digest = hashlib.sha256(
            (
                self.device_id
                + "\x1f"
                + self.binding_id
                + "\x1f"
                + str(self.binding_epoch)
            ).encode("utf-8")
        ).hexdigest()
        return f"mcp-{digest[:32]}"

    def require_scope(self, scope: str) -> None:
        if scope not in self.scopes:
            raise ExecutionTokenError("Execution token scope is insufficient")


class ExecutionTokenCodec:
    """Issue and verify compact HMAC tokens without persisting bearer secrets."""

    def __init__(
        self,
        secret: str,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        encoded = secret.encode("utf-8")
        if len(encoded) < 32:
            raise ValueError("Arena MCP token secret must contain at least 32 bytes")
        self._secret = encoded
        self._clock = clock or time.time

    def issue(
        self,
        *,
        device_id: str,
        binding_id: str,
        binding_epoch: int,
        scopes: Iterable[str] = MCP_SCOPES,
        ttl_seconds: int = 15 * 60,
    ) -> tuple[str, ExecutionTokenClaims]:
        if ttl_seconds < 30 or ttl_seconds > 15 * 60:
            raise ValueError("Arena MCP token TTL must be between 30 and 900 seconds")
        resolved_scopes = tuple(sorted(set(scopes)))
        if not resolved_scopes or not set(resolved_scopes).issubset(MCP_SCOPES):
            raise ValueError("Arena MCP token scopes are invalid")
        issued_at = int(self._clock())
        claims = ExecutionTokenClaims(
            version=MCP_TOKEN_VERSION,
            issuer="arena402",
            subject=f"device:{device_id}",
            audience=MCP_AUDIENCE,
            device_id=device_id,
            binding_id=binding_id,
            binding_epoch=binding_epoch,
            scopes=resolved_scopes,
            issued_at=issued_at,
            expires_at=issued_at + ttl_seconds,
            token_id=f"mcp-token-{secrets.token_hex(12)}",
        )
        payload = _canonical_json(claims.model_dump(mode="json"))
        encoded_payload = _base64url_encode(payload)
        signature = hmac.new(
            self._secret,
            f"{MCP_TOKEN_VERSION}.{encoded_payload}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        token = (
            f"{MCP_TOKEN_VERSION}.{encoded_payload}." f"{_base64url_encode(signature)}"
        )
        return token, claims

    def decode(self, token: str) -> ExecutionTokenClaims:
        if not token or len(token) > 4096:
            raise ExecutionTokenError("Execution token is invalid")
        prefix = f"{MCP_TOKEN_VERSION}."
        if not token.startswith(prefix):
            raise ExecutionTokenError("Execution token version is unsupported")
        try:
            encoded_payload, encoded_signature = token[len(prefix) :].rsplit(
                ".",
                1,
            )
        except ValueError as exc:
            raise ExecutionTokenError("Execution token is invalid") from exc
        expected = hmac.new(
            self._secret,
            f"{MCP_TOKEN_VERSION}.{encoded_payload}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        try:
            supplied = _base64url_decode(encoded_signature)
        except ValueError as exc:
            raise ExecutionTokenError("Execution token is invalid") from exc
        if not hmac.compare_digest(expected, supplied):
            raise ExecutionTokenError("Execution token is invalid")
        try:
            payload = json.loads(_base64url_decode(encoded_payload))
            claims = ExecutionTokenClaims.model_validate(payload)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ExecutionTokenError("Execution token is invalid") from exc
        now = int(self._clock())
        if claims.issued_at > now + 30:
            raise ExecutionTokenError("Execution token issue time is invalid")
        if claims.expires_at <= now:
            raise ExecutionTokenError("Execution token has expired")
        if claims.expires_at - claims.issued_at > 15 * 60:
            raise ExecutionTokenError("Execution token lifetime is invalid")
        if not set(claims.scopes).issubset(MCP_SCOPES):
            raise ExecutionTokenError("Execution token scopes are invalid")
        return claims


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    if not value or any(char.isspace() for char in value):
        raise ValueError("invalid base64url")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid base64url") from exc
    if _base64url_encode(decoded) != value:
        raise ValueError("invalid base64url")
    return decoded


__all__ = [
    "ExecutionTokenClaims",
    "ExecutionTokenCodec",
    "ExecutionTokenError",
    "MCP_AUDIENCE",
    "MCP_SCOPES",
]
