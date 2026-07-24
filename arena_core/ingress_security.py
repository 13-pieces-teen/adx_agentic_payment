"""Fail-closed security checks for Arena-owned persistence ingress.

This module deliberately has no repository or logging dependency. Callers can
therefore run it before constructing SQL parameters, events, traces, or error
contexts. Rejections expose a stable reason code but never echo the rejected
value.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Final, Literal, TypeAlias

from arena_agent_contracts import AgentTaskResultV1, BuyAction, SellAction


IngressRejectionCode: TypeAlias = Literal[
    "secret_bearing_key",
    "secret_or_pii",
    "unsafe_binary",
    "unsafe_config_key",
    "unsafe_config_value",
    "unsafe_unicode_control",
    "unsafe_unicode_format",
]

_MAX_CONFIG_DEPTH: Final[int] = 16
_MAX_CONFIG_NODES: Final[int] = 4096

# Credential references are identifiers only. They may point at an external
# secret but must never contain the secret itself.
_SAFE_REFERENCE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "credentialid",
        "credentialref",
        "credentialreference",
        "secretref",
        "secretreference",
    }
)

_SECRET_KEY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "accesstoken",
        "apikey",
        "apisecret",
        "authorization",
        "bearer",
        "clientsecret",
        "credential",
        "password",
        "passwd",
        "privatekey",
        "refreshtoken",
        "seedphrase",
        "secret",
        "secretkey",
        "token",
        "walletprivatekey",
        "xapikey",
    }
)

_SECRET_KEY_SUFFIXES: Final[tuple[str, ...]] = (
    "accesstoken",
    "apikey",
    "apisecret",
    "authorization",
    "bearer",
    "clientsecret",
    "credential",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "seedphrase",
    "secretkey",
    "token",
)

_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # OpenAI/Anthropic and similarly shaped provider keys.
    re.compile(r"\bsk-[a-z0-9_-]{12,}(?=$|[^a-z0-9_])", re.IGNORECASE),
    # Google API keys.
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    # AWS-style access key ids and Tencent Cloud SecretIds.
    re.compile(r"\b(?:AKIA[0-9A-Z]{16}|AKID[0-9A-Za-z]{16,})\b"),
    # Common GitHub and Slack token formats.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", re.IGNORECASE),
    # Labeled credentials catch provider-specific formats without attempting
    # to classify every possible high-entropy string.
    re.compile(
        r"\b(?:api[-_ ]?key|api[-_ ]?secret|authorization|client[-_ ]?secret|"
        r"private[-_ ]?key|secret[-_ ]?key)\b\s*[:=]?\s*"
        r"(?:bearer\s+)?[a-z0-9._~+/=-]{8,}",
        re.IGNORECASE,
    ),
    # JWTs and 32-byte hexadecimal private keys.
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    ),
    re.compile(r"\b(?:0x)?[a-f0-9]{64}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:seed phrase|mnemonic|private key|助记词|私钥)\b",
        re.IGNORECASE,
    ),
)

_PII_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\b",
        re.IGNORECASE,
    ),
)

# Bidirectional overrides/isolates and zero-width characters are all Cf, but
# keeping the explicit set documents the threat and protects the behavior if
# Unicode category handling changes.
_BIDI_AND_ZERO_WIDTH: Final[frozenset[str]] = frozenset(
    {
        "\u061c",  # Arabic letter mark
        "\u200b",  # zero width space
        "\u200c",  # zero width non-joiner
        "\u200d",  # zero width joiner
        "\u200e",  # left-to-right mark
        "\u200f",  # right-to-left mark
        "\u202a",  # left-to-right embedding
        "\u202b",  # right-to-left embedding
        "\u202c",  # pop directional formatting
        "\u202d",  # left-to-right override
        "\u202e",  # right-to-left override
        "\u2060",  # word joiner
        "\u2066",  # left-to-right isolate
        "\u2067",  # right-to-left isolate
        "\u2068",  # first strong isolate
        "\u2069",  # pop directional isolate
        "\ufeff",  # zero width no-break space / BOM
    }
)


class ArenaIngressSecurityError(ValueError):
    """A safe-to-log ingress rejection.

    ``value`` is intentionally not accepted or retained. The exception's
    ``str`` and ``repr`` therefore cannot accidentally expose rejected input.
    """

    def __init__(self, code: IngressRejectionCode) -> None:
        self.code = code
        super().__init__(f"Arena ingress rejected input ({code})")


@dataclass(slots=True)
class _ScanBudget:
    nodes: int = 0

    def consume(self) -> None:
        self.nodes += 1
        if self.nodes > _MAX_CONFIG_NODES:
            # Treat an oversized object as an unsafe config shape without
            # adding a separate externally observable value-dependent error.
            raise ArenaIngressSecurityError("unsafe_config_key")


def unsafe_unicode_reason(value: str) -> IngressRejectionCode | None:
    """Return a safe reason for invisible/control Unicode, if present."""

    for character in value:
        if character in _BIDI_AND_ZERO_WIDTH:
            return "unsafe_unicode_format"
        category = unicodedata.category(character)
        if category in {"Cf", "Cs"}:
            return "unsafe_unicode_format"
        if category == "Cc":
            return "unsafe_unicode_control"
    return None


def sensitive_text_reason(
    value: str, *, include_pii: bool = True
) -> IngressRejectionCode | None:
    """Classify obvious secrets/PII without returning the source text."""

    normalized = unicodedata.normalize("NFKC", value)
    if any(pattern.search(normalized) for pattern in _SECRET_PATTERNS):
        return "secret_or_pii"
    if include_pii and any(pattern.search(normalized) for pattern in _PII_PATTERNS):
        return "secret_or_pii"
    return None


def validate_runtime_controlled_text(
    value: str, *, include_pii: bool = True
) -> str:
    """Reject unsafe Runtime-controlled text before any durable write.

    The original value is returned unchanged on success so identifiers retain
    their idempotency semantics. On failure, the exception contains only a
    stable reason code. Callers must not include ``value`` in their own error
    or Event payload.
    """

    unicode_reason = unsafe_unicode_reason(value)
    if unicode_reason is not None:
        raise ArenaIngressSecurityError(unicode_reason)
    sensitive_reason = sensitive_text_reason(value, include_pii=include_pii)
    if sensitive_reason is not None:
        raise ArenaIngressSecurityError(sensitive_reason)
    return value


def validate_runtime_result_identifiers(
    result: AgentTaskResultV1,
) -> AgentTaskResultV1:
    """Validate every Runtime-controlled identifier before persistence."""

    validate_runtime_controlled_text(result.result_id)
    if isinstance(result.action, (BuyAction, SellAction)):
        validate_runtime_controlled_text(result.action.good)
    return result


def secure_config_snapshot(config_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached config snapshot that is safe for persistence.

    Secret-bearing field names are rejected even when their current value is
    empty. Opaque credential/secret *references* are allowed only under the
    explicit reference keys above, and their values are still scanned to stop
    callers from placing a raw credential in a reference field.
    """

    if not isinstance(config_snapshot, Mapping):
        raise ArenaIngressSecurityError("unsafe_config_key")

    detached = copy.deepcopy(dict(config_snapshot))
    _scan_config_value(detached, depth=0, budget=_ScanBudget())
    return detached


def _scan_config_value(value: Any, *, depth: int, budget: _ScanBudget) -> None:
    budget.consume()
    if depth > _MAX_CONFIG_DEPTH:
        raise ArenaIngressSecurityError("unsafe_config_key")

    if isinstance(value, str):
        validate_runtime_controlled_text(value)
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ArenaIngressSecurityError("unsafe_binary")
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ArenaIngressSecurityError("unsafe_config_key")
            key_reason = unsafe_unicode_reason(key)
            if key_reason is not None:
                raise ArenaIngressSecurityError(key_reason)
            if sensitive_text_reason(key) is not None:
                raise ArenaIngressSecurityError("secret_bearing_key")

            normalized_key = _normalize_key(key)
            if _is_secret_bearing_key(normalized_key):
                raise ArenaIngressSecurityError("secret_bearing_key")

            # Explicit reference names bypass only the key-name rejection.
            # Their values still take the normal sensitive-text path.
            _scan_config_value(nested, depth=depth + 1, budget=budget)
        return
    if isinstance(value, Sequence):
        for nested in value:
            _scan_config_value(nested, depth=depth + 1, budget=budget)
        return
    if value is None or isinstance(value, (bool, int, Decimal, date, datetime)):
        return
    if isinstance(value, Enum):
        _scan_config_value(value.value, depth=depth + 1, budget=budget)
        return
    raise ArenaIngressSecurityError("unsafe_config_value")


def _normalize_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _is_secret_bearing_key(normalized_key: str) -> bool:
    if normalized_key in _SAFE_REFERENCE_KEYS:
        return False
    if normalized_key in _SECRET_KEY_NAMES:
        return True
    return normalized_key.endswith(_SECRET_KEY_SUFFIXES)


__all__ = [
    "ArenaIngressSecurityError",
    "IngressRejectionCode",
    "secure_config_snapshot",
    "sensitive_text_reason",
    "unsafe_unicode_reason",
    "validate_runtime_controlled_text",
    "validate_runtime_result_identifiers",
]
