"""Safe provider adapter contracts for Hosted Arena Agent model calls."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal, Protocol, TypeAlias, cast, runtime_checkable

from arena_core.ingress_security import (
    ArenaIngressSecurityError,
    validate_runtime_controlled_text,
)
from hosted_agent_runtime.secret_store import WorkerSecret


ProviderErrorCode: TypeAlias = Literal[
    "adapter_mismatch",
    "authentication_failed",
    "invalid_json",
    "invalid_structured_output",
    "permanent_request",
    "provider_unavailable",
    "rate_limited",
    "request_outcome_unknown",
    "script_exhausted",
    "transport_failure_before_send",
]

MAX_POSTGRES_BIGINT: Final[int] = (1 << 63) - 1
_PROVIDER_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "adapter_mismatch",
        "authentication_failed",
        "invalid_json",
        "invalid_structured_output",
        "permanent_request",
        "provider_unavailable",
        "rate_limited",
        "request_outcome_unknown",
        "script_exhausted",
        "transport_failure_before_send",
    }
)
_TRANSIENT_ERROR_CODES = frozenset(
    {
        "provider_unavailable",
        "rate_limited",
        "transport_failure_before_send",
    }
)
_INVALID_OUTPUT_ERROR_CODES = frozenset(
    {
        "invalid_json",
        "invalid_structured_output",
    }
)
_SAFE_IDENTIFIER_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$"
)
_SAFE_REQUEST_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
)


class ProviderInvocationError(RuntimeError):
    """Safe provider failure which never retains a raw body or exception."""

    def __init__(self, code: ProviderErrorCode) -> None:
        self.code = validate_provider_error_code(code)
        super().__init__(
            f"Hosted provider invocation failed ({self.code})"
        )

    @property
    def retryable(self) -> bool:
        return self.code in _TRANSIENT_ERROR_CODES

    @property
    def invalid_output(self) -> bool:
        return self.code in _INVALID_OUTPUT_ERROR_CODES

    @property
    def outcome_unknown(self) -> bool:
        return self.code == "request_outcome_unknown"


def _validate_non_negative_int(value: int) -> None:
    if (
        type(value) is not int
        or value < 0
        or value > MAX_POSTGRES_BIGINT
    ):
        raise ValueError(
            "usage values must fit a non-negative PostgreSQL BIGINT"
        )


def validate_provider_error_code(value: object) -> ProviderErrorCode:
    """Enforce the safe error-code closed set at runtime.

    ``Literal`` protects static callers only. A real adapter can still pass an
    arbitrary runtime string, so rejection must happen before the value can
    reach an exception, log, Attempt record, or database parameter.
    """

    if type(value) is not str or value not in _PROVIDER_ERROR_CODES:
        raise ValueError("provider error code is invalid")
    return cast(ProviderErrorCode, value)


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Normalized numeric usage without text, payloads, or inferred values."""

    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    reasoning_tokens: int | None
    complete: bool

    def __post_init__(self) -> None:
        for value in (
            self.input_tokens,
            self.output_tokens,
            self.cached_input_tokens,
            self.reasoning_tokens,
        ):
            if value is not None:
                _validate_non_negative_int(value)
        if type(self.complete) is not bool:
            raise ValueError("usage complete must be a boolean")
        if self.complete and any(
            value is None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cached_input_tokens,
                self.reasoning_tokens,
            )
        ):
            raise ValueError("complete usage requires every numeric field")

    @classmethod
    def incomplete(cls) -> "ProviderUsage":
        """Represent absent provider usage without fabricating token counts."""

        return cls(
            input_tokens=None,
            output_tokens=None,
            cached_input_tokens=None,
            reasoning_tokens=None,
            complete=False,
        )


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """One allowlisted structured model request.

    The request deliberately has no credential, endpoint, header, raw body, or
    private reasoning field. The credential is a separate redacted argument to
    ``ProviderAdapter.invoke``.
    """

    attempt_id: str
    task_id: str
    task_kind: Literal[
        "arena.decide",
        "arena.negotiate",
        "arena.market.intent",
        "arena.market.rfq",
        "arena.market.select",
    ]
    idempotency_key: str
    model_id: str
    prompt_version: str
    context_version: str
    output_version: str
    system_instructions: str = field(repr=False)
    input_json: str = field(repr=False)
    output_schema_json: str = field(repr=False)
    thinking_enabled: bool
    thinking_parameter_name: str | None
    max_output_tokens: int
    request_timeout_ms: int

    def __post_init__(self) -> None:
        for value in (
            self.attempt_id,
            self.task_id,
            self.model_id,
            self.prompt_version,
            self.context_version,
            self.output_version,
        ):
            if (
                type(value) is not str
                or not _SAFE_IDENTIFIER_PATTERN.fullmatch(value)
            ):
                raise ValueError("provider request contains an invalid identifier")
        if type(self.idempotency_key) is not str or not self.idempotency_key:
            raise ValueError("provider request requires an idempotency key")
        for value in (
            self.system_instructions,
            self.input_json,
            self.output_schema_json,
        ):
            if type(value) is not str or not value:
                raise ValueError("provider request prompt fields must be non-empty")
        if type(self.thinking_enabled) is not bool:
            raise ValueError("thinking enabled must be a boolean")
        if self.thinking_parameter_name is not None and (
            type(self.thinking_parameter_name) is not str
            or not self.thinking_parameter_name
        ):
            raise ValueError("thinking parameter name must be non-empty")
        if (
            type(self.max_output_tokens) is not int
            or self.max_output_tokens <= 0
            or type(self.request_timeout_ms) is not int
            or self.request_timeout_ms <= 0
        ):
            raise ValueError("provider request limits must be positive integers")


def _copy_json_value(value: object) -> object:
    """Copy a parsed JSON value while rejecting opaque/raw response objects."""

    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, list):
        return [_copy_json_value(item) for item in value]
    if isinstance(value, Mapping):
        copied: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("structured output keys must be strings")
            copied[key] = _copy_json_value(item)
        return copied
    # Structured Arena action values are strings, integers, booleans and null.
    # In particular, bytes/raw bodies and binary floating point are rejected.
    raise ValueError("structured output must contain JSON-safe values")


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """Sanitized provider parse result.

    A concrete adapter may observe additional provider fields while parsing,
    but it must discard reasoning text, encrypted reasoning blobs, and the raw
    body before this object is created.
    """

    structured_output: Mapping[str, object] = field(repr=False)
    usage: ProviderUsage
    provider_request_id: str | None = None
    actual_model: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.structured_output, Mapping):
            raise ValueError("structured output must be an object")
        copied = _copy_json_value(copy.deepcopy(dict(self.structured_output)))
        assert isinstance(copied, dict)
        object.__setattr__(
            self,
            "structured_output",
            MappingProxyType(copied),
        )
        if not isinstance(self.usage, ProviderUsage):
            raise ValueError("provider response requires normalized usage")
        if self.provider_request_id is not None:
            validate_provider_request_id(self.provider_request_id)
        if self.actual_model is not None and (
            type(self.actual_model) is not str
            or not _SAFE_IDENTIFIER_PATTERN.fullmatch(self.actual_model)
        ):
            raise ValueError("provider actual model is not safe to persist")


def validate_provider_request_id(value: str) -> str:
    """Reject unsafe Provider ids without echoing attacker-controlled text."""

    if (
        type(value) is not str
        or not _SAFE_REQUEST_ID_PATTERN.fullmatch(value)
    ):
        raise ValueError("provider request id is not safe to persist")
    try:
        validate_runtime_controlled_text(value)
    except ArenaIngressSecurityError:
        raise ValueError(
            "provider request id is not safe to persist"
        ) from None
    return value


@runtime_checkable
class ProviderAdapter(Protocol):
    """Single-provider adapter; selection and fallback are outside adapters."""

    @property
    def adapter_id(self) -> str: ...

    async def invoke(
        self,
        request: ProviderRequest,
        credential: WorkerSecret,
    ) -> ProviderResponse:
        """Return a sanitized structured response or one safe error code."""


__all__ = [
    "ProviderAdapter",
    "ProviderErrorCode",
    "ProviderInvocationError",
    "MAX_POSTGRES_BIGINT",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderUsage",
    "validate_provider_error_code",
    "validate_provider_request_id",
]
