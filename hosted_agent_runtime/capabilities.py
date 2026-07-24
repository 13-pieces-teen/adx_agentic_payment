"""Fail-closed Provider/Model capability registry for Hosted Arena Agents.

This registry contains only execution metadata.  It deliberately has no
endpoint, credential, prompt, reasoning text, or chain-of-thought fields.
Users may select whether thinking is enabled when the model supports it, but
they cannot set a cross-provider effort value: the provider/model default is
always used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from enum import Enum
from types import MappingProxyType
from typing import Final, Iterable, Literal, Mapping, TypeAlias


CapabilityErrorCode: TypeAlias = Literal[
    "duplicate_capability",
    "invalid_capability",
    "model_not_available",
    "mutable_model_alias",
    "output_token_limit_exceeded",
    "thinking_always_on",
    "thinking_not_supported",
]

CAPABILITY_SCHEMA_VERSION_V1: Final[str] = "arena.provider-capability.v1"
DEFAULT_REGISTRY_VERSION_V1: Final[str] = "arena.provider-registry.v1"

_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$"
)
_PARAMETER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z][A-Za-z0-9_.]{0,127}$"
)
_MUTABLE_ALIAS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[-_.:/])(latest|current)(?:$|[-_.:/])",
    re.IGNORECASE,
)
_VISIBLE_REASONING_PARAMETER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(chain[_.-]?of[_.-]?thought|cot(?:[_.-]|$)|"
    r"reasoning[_.-]?(?:text|content|trace)|"
    r"thinking[_.-]?(?:text|content|trace)|"
    r"include[_.-]?thoughts?|show[_.-]?(?:thoughts?|reasoning)|"
    r"encrypted[_.-]?reasoning)",
    re.IGNORECASE,
)


class CapabilityError(ValueError):
    """Safe capability definition or selection error."""

    def __init__(self, code: CapabilityErrorCode) -> None:
        self.code = code
        super().__init__(f"Hosted model capability rejected ({code})")


class ThinkingMode(str, Enum):
    UNSUPPORTED = "unsupported"
    OPTIONAL = "optional"
    ALWAYS_ON = "always_on"


class ThinkingEffortPolicy(str, Enum):
    """MVP never accepts a user-selected cross-provider reasoning intensity."""

    PROVIDER_DEFAULT = "provider_default"


@dataclass(frozen=True, slots=True)
class ModelCapability:
    provider_id: str
    adapter_id: str
    model_id: str
    display_name: str
    supports_structured_output: bool
    thinking_mode: ThinkingMode
    thinking_parameter_name: str | None
    max_output_tokens: int
    request_timeout_cap_ms: int
    adapter_version: str
    schema_version: str = CAPABILITY_SCHEMA_VERSION_V1
    immutable_model_id: bool = True
    verified: bool = False
    enabled: bool = False
    thinking_effort_policy: ThinkingEffortPolicy = (
        ThinkingEffortPolicy.PROVIDER_DEFAULT
    )

    def __post_init__(self) -> None:
        for value in (
            self.provider_id,
            self.adapter_id,
            self.model_id,
            self.adapter_version,
            self.schema_version,
        ):
            if type(value) is not str or not _IDENTIFIER_PATTERN.fullmatch(value):
                raise CapabilityError("invalid_capability")

        if (
            type(self.display_name) is not str
            or not self.display_name.strip()
            or len(self.display_name) > 128
        ):
            raise CapabilityError("invalid_capability")
        if _MUTABLE_ALIAS_PATTERN.search(self.model_id):
            raise CapabilityError("mutable_model_alias")
        if type(self.supports_structured_output) is not bool:
            raise CapabilityError("invalid_capability")
        if not isinstance(self.thinking_mode, ThinkingMode):
            raise CapabilityError("invalid_capability")
        if not isinstance(
            self.thinking_effort_policy, ThinkingEffortPolicy
        ):
            raise CapabilityError("invalid_capability")
        if (
            type(self.max_output_tokens) is not int
            or self.max_output_tokens <= 0
            or self.max_output_tokens > 1_000_000
            or type(self.request_timeout_cap_ms) is not int
            or self.request_timeout_cap_ms <= 0
            or self.request_timeout_cap_ms > 1_800_000
        ):
            raise CapabilityError("invalid_capability")
        if type(self.immutable_model_id) is not bool:
            raise CapabilityError("invalid_capability")
        if type(self.verified) is not bool or type(self.enabled) is not bool:
            raise CapabilityError("invalid_capability")

        parameter_name = self.thinking_parameter_name
        if parameter_name is not None:
            if (
                type(parameter_name) is not str
                or not _PARAMETER_PATTERN.fullmatch(parameter_name)
                or _VISIBLE_REASONING_PARAMETER_PATTERN.search(parameter_name)
            ):
                raise CapabilityError("invalid_capability")

        if (
            self.thinking_mode is ThinkingMode.UNSUPPORTED
            and parameter_name is not None
        ):
            raise CapabilityError("invalid_capability")
        if (
            self.thinking_mode is ThinkingMode.OPTIONAL
            and parameter_name is None
        ):
            raise CapabilityError("invalid_capability")

        if self.enabled and (
            not self.verified
            or not self.immutable_model_id
            or not self.supports_structured_output
        ):
            raise CapabilityError("invalid_capability")

    def public_view(self) -> "PublicModelCapability":
        if not self.enabled or not self.verified:
            raise CapabilityError("model_not_available")
        return PublicModelCapability(
            provider_id=self.provider_id,
            model_id=self.model_id,
            display_name=self.display_name,
            supports_structured_output=self.supports_structured_output,
            thinking_mode=self.thinking_mode,
            max_output_tokens=self.max_output_tokens,
            request_timeout_cap_ms=self.request_timeout_cap_ms,
            schema_version=self.schema_version,
        )


@dataclass(frozen=True, slots=True)
class PublicModelCapability:
    """Safe UI/API projection; adapter parameter names remain server-side."""

    provider_id: str
    model_id: str
    display_name: str
    supports_structured_output: bool
    thinking_mode: ThinkingMode
    max_output_tokens: int
    request_timeout_cap_ms: int
    schema_version: str

    @property
    def thinking_can_toggle(self) -> bool:
        return self.thinking_mode is ThinkingMode.OPTIONAL

    @property
    def effective_thinking_default(self) -> bool:
        return self.thinking_mode is ThinkingMode.ALWAYS_ON

    def to_dict(self) -> dict[str, object]:
        return {
            "providerId": self.provider_id,
            "modelId": self.model_id,
            "displayName": self.display_name,
            "supportsStructuredOutput": self.supports_structured_output,
            "thinkingMode": self.thinking_mode.value,
            "thinkingCanToggle": self.thinking_can_toggle,
            "effectiveThinkingDefault": self.effective_thinking_default,
            "maxOutputTokens": self.max_output_tokens,
            "requestTimeoutCapMs": self.request_timeout_cap_ms,
            "schemaVersion": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ResolvedModelCapability:
    provider_id: str
    adapter_id: str
    model_id: str
    adapter_version: str
    schema_version: str
    thinking_mode: ThinkingMode
    thinking_enabled: bool
    thinking_parameter_name: str | None
    thinking_effort_policy: ThinkingEffortPolicy
    max_output_tokens: int
    request_timeout_ms: int


def _validate_registry_version(value: str) -> str:
    if type(value) is not str or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise CapabilityError("invalid_capability")
    return value


class CapabilityRegistry:
    """Immutable allowlist; enabled entries must already be truly verified."""

    def __init__(
        self,
        capabilities: Iterable[ModelCapability] = (),
        *,
        registry_version: str = DEFAULT_REGISTRY_VERSION_V1,
    ) -> None:
        self._registry_version = _validate_registry_version(registry_version)
        entries: dict[tuple[str, str], ModelCapability] = {}
        for capability in capabilities:
            if not isinstance(capability, ModelCapability):
                raise CapabilityError("invalid_capability")
            key = (capability.provider_id, capability.model_id)
            if key in entries:
                raise CapabilityError("duplicate_capability")
            entries[key] = capability
        self._entries: Mapping[
            tuple[str, str], ModelCapability
        ] = MappingProxyType(entries)

    @property
    def registry_version(self) -> str:
        return self._registry_version

    def list_public(self) -> tuple[PublicModelCapability, ...]:
        enabled = (
            capability.public_view()
            for capability in self._entries.values()
            if capability.enabled and capability.verified
        )
        return tuple(
            sorted(
                enabled,
                key=lambda item: (item.provider_id, item.model_id),
            )
        )

    def resolve(
        self,
        *,
        provider_id: str,
        model_id: str,
        thinking_enabled: bool,
        remaining_timeout_ms: int,
        requested_max_output_tokens: int | None = None,
    ) -> ResolvedModelCapability:
        if (
            type(provider_id) is not str
            or type(model_id) is not str
            or type(thinking_enabled) is not bool
            or type(remaining_timeout_ms) is not int
            or remaining_timeout_ms <= 0
        ):
            raise CapabilityError("model_not_available")

        capability = self._entries.get((provider_id, model_id))
        if (
            capability is None
            or not capability.enabled
            or not capability.verified
        ):
            raise CapabilityError("model_not_available")

        if requested_max_output_tokens is None:
            output_tokens = capability.max_output_tokens
        elif (
            type(requested_max_output_tokens) is not int
            or requested_max_output_tokens <= 0
            or requested_max_output_tokens > capability.max_output_tokens
        ):
            raise CapabilityError("output_token_limit_exceeded")
        else:
            output_tokens = requested_max_output_tokens

        if (
            capability.thinking_mode is ThinkingMode.UNSUPPORTED
            and thinking_enabled
        ):
            raise CapabilityError("thinking_not_supported")
        if (
            capability.thinking_mode is ThinkingMode.ALWAYS_ON
            and not thinking_enabled
        ):
            raise CapabilityError("thinking_always_on")

        effective_thinking = (
            capability.thinking_mode is ThinkingMode.ALWAYS_ON
            or thinking_enabled
        )
        parameter_name = (
            capability.thinking_parameter_name
            if capability.thinking_mode is ThinkingMode.OPTIONAL
            else None
        )

        return ResolvedModelCapability(
            provider_id=capability.provider_id,
            adapter_id=capability.adapter_id,
            model_id=capability.model_id,
            adapter_version=capability.adapter_version,
            schema_version=capability.schema_version,
            thinking_mode=capability.thinking_mode,
            thinking_enabled=effective_thinking,
            thinking_parameter_name=parameter_name,
            thinking_effort_policy=ThinkingEffortPolicy.PROVIDER_DEFAULT,
            max_output_tokens=output_tokens,
            request_timeout_ms=min(
                remaining_timeout_ms,
                capability.request_timeout_cap_ms,
            ),
        )


def assert_no_private_reasoning_fields() -> None:
    """Regression guard for accidental CoT/reasoning payload fields."""

    forbidden = _VISIBLE_REASONING_PARAMETER_PATTERN
    for model in (
        ModelCapability,
        PublicModelCapability,
        ResolvedModelCapability,
    ):
        for field in fields(model):
            if forbidden.search(field.name):
                raise CapabilityError("invalid_capability")


assert_no_private_reasoning_fields()


__all__ = [
    "CAPABILITY_SCHEMA_VERSION_V1",
    "DEFAULT_REGISTRY_VERSION_V1",
    "CapabilityError",
    "CapabilityRegistry",
    "ModelCapability",
    "PublicModelCapability",
    "ResolvedModelCapability",
    "ThinkingEffortPolicy",
    "ThinkingMode",
    "assert_no_private_reasoning_fields",
]
