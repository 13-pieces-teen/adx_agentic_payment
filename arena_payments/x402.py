"""Small x402 V2 HTTP envelope codec with Arena intent binding."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any


class X402ProtocolError(ValueError):
    pass


def encode_x402_header(value: dict[str, Any]) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def decode_x402_header(value: str, *, max_bytes: int = 32_768) -> dict[str, Any]:
    if not value or len(value) > max_bytes * 2:
        raise X402ProtocolError("invalid_x402_header")
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) > max_bytes:
            raise X402ProtocolError("invalid_x402_header")
        decoded = json.loads(raw)
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise X402ProtocolError("invalid_x402_header") from exc
    if not isinstance(decoded, dict):
        raise X402ProtocolError("invalid_x402_header")
    return decoded
