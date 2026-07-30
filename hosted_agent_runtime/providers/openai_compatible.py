"""Production OpenAI-compatible Chat Completions provider adapter.

Endpoints are deployment-owned allowlist entries. A user supplies only an API
key and selects a registered provider/model; request data can never redirect a
credential to an arbitrary host.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

import httpx

from hosted_agent_runtime.secret_store import WorkerSecret

from .base import (
    ProviderInvocationError,
    ProviderRequest,
    ProviderResponse,
    ProviderUsage,
)


_MAX_RESPONSE_BYTES: Final[int] = 1_048_576
_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"https"})
_HOSTNAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$"
)
ResponseFormatMode = Literal["json_schema", "json_object"]
ThinkingDialect = Literal["none", "deepseek"]


@dataclass(frozen=True, slots=True)
class OpenAICompatibleSettings:
    adapter_id: str
    endpoint: str
    response_format_mode: ResponseFormatMode = "json_object"
    thinking_dialect: ThinkingDialect = "none"
    allow_http_hostname: str | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        allowed_http_hostname = self.allow_http_hostname
        if allowed_http_hostname is not None and (
            not _HOSTNAME_PATTERN.fullmatch(allowed_http_hostname)
            or allowed_http_hostname != allowed_http_hostname.lower()
        ):
            raise ValueError("invalid OpenAI-compatible provider settings")
        scheme_allowed = parsed.scheme in _ALLOWED_SCHEMES or (
            parsed.scheme == "http"
            and allowed_http_hostname is not None
            and parsed.hostname == allowed_http_hostname
        )
        if (
            not self.adapter_id
            or not scheme_allowed
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or self.response_format_mode not in {"json_schema", "json_object"}
            or self.thinking_dialect not in {"none", "deepseek"}
        ):
            raise ValueError("invalid OpenAI-compatible provider settings")


class OpenAICompatibleChatAdapter:
    """Small HTTP adapter that emits only sanitized response metadata."""

    def __init__(
        self,
        settings: OpenAICompatibleSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not isinstance(settings, OpenAICompatibleSettings):
            raise TypeError("settings must be OpenAICompatibleSettings")
        self._settings = settings
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )

    @property
    def adapter_id(self) -> str:
        return self._settings.adapter_id

    async def close(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def invoke(
        self,
        request: ProviderRequest,
        credential: WorkerSecret,
    ) -> ProviderResponse:
        if not isinstance(request, ProviderRequest):
            raise ProviderInvocationError("permanent_request")
        if not isinstance(credential, WorkerSecret):
            raise ProviderInvocationError("authentication_failed")

        body = self._request_body(request)
        key = credential.reveal_for_worker()
        try:
            async with self._client.stream(
                "POST",
                self._settings.endpoint,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=body,
                timeout=httpx.Timeout(
                    request.request_timeout_ms / 1000,
                    connect=min(10.0, request.request_timeout_ms / 1000),
                ),
            ) as response:
                if response.status_code in {401, 403}:
                    raise ProviderInvocationError("authentication_failed")
                if response.status_code == 429:
                    raise ProviderInvocationError("rate_limited")
                if response.status_code >= 500:
                    raise ProviderInvocationError("provider_unavailable")
                if response.status_code >= 400:
                    raise ProviderInvocationError("permanent_request")

                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > _MAX_RESPONSE_BYTES:
                        raise ProviderInvocationError("invalid_json")
                try:
                    payload = json.loads(content)
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                    raise ProviderInvocationError("invalid_json") from None
                return self._parse_response(payload, response)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise ProviderInvocationError(
                "transport_failure_before_send"
            ) from None
        except (
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.WriteError,
            httpx.WriteTimeout,
            httpx.RemoteProtocolError,
        ):
            raise ProviderInvocationError(
                "request_outcome_unknown"
            ) from None
        except httpx.HTTPError:
            raise ProviderInvocationError(
                "request_outcome_unknown"
            ) from None
        finally:
            key = ""

    def _request_body(self, request: ProviderRequest) -> dict[str, object]:
        try:
            output_schema = json.loads(request.output_schema_json)
        except json.JSONDecodeError:
            raise ProviderInvocationError("permanent_request") from None

        if self._settings.response_format_mode == "json_schema":
            response_format: dict[str, object] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "arena_agent_action",
                    "strict": True,
                    "schema": output_schema,
                },
            }
        else:
            response_format = {"type": "json_object"}

        system_instructions = request.system_instructions
        if self._settings.response_format_mode == "json_object":
            system_instructions = (
                f"{system_instructions}\n"
                "Trusted outputSchema (return exactly one matching object): "
                f"{json.dumps(output_schema, separators=(',', ':'), sort_keys=True)}"
            )

        body: dict[str, object] = {
            "model": request.model_id,
            "messages": [
                {
                    "role": "system",
                    "content": system_instructions,
                },
                {"role": "user", "content": request.input_json},
            ],
            "response_format": response_format,
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        if self._settings.thinking_dialect == "deepseek":
            body["thinking"] = {
                "type": "enabled" if request.thinking_enabled else "disabled"
            }
        return body

    @staticmethod
    def _parse_response(
        payload: object,
        response: httpx.Response,
    ) -> ProviderResponse:
        try:
            assert isinstance(payload, dict)
            choices = payload["choices"]
            assert isinstance(choices, list) and len(choices) == 1
            choice = choices[0]
            assert isinstance(choice, dict)
            message = choice["message"]
            assert isinstance(message, dict)
            content = message["content"]
            assert isinstance(content, str)
            structured = json.loads(content)
            assert isinstance(structured, dict)
        except (
            AssertionError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            raise ProviderInvocationError(
                "invalid_structured_output"
            ) from None

        usage = OpenAICompatibleChatAdapter._usage(payload.get("usage"))
        request_id = response.headers.get("x-request-id")
        actual_model = payload.get("model")
        if not isinstance(actual_model, str):
            actual_model = None
        try:
            return ProviderResponse(
                structured_output=structured,
                usage=usage,
                provider_request_id=request_id,
                actual_model=actual_model,
            )
        except ValueError:
            raise ProviderInvocationError(
                "invalid_structured_output"
            ) from None

    @staticmethod
    def _usage(value: object) -> ProviderUsage:
        if not isinstance(value, dict):
            return ProviderUsage.incomplete()
        try:
            input_tokens = value["prompt_tokens"]
            output_tokens = value["completion_tokens"]
            prompt_details = value.get("prompt_tokens_details") or {}
            completion_details = value.get("completion_tokens_details") or {}
            assert isinstance(input_tokens, int) and not isinstance(
                input_tokens, bool
            )
            assert isinstance(output_tokens, int) and not isinstance(
                output_tokens, bool
            )
            cached = value.get("prompt_cache_hit_tokens")
            if cached is None:
                cached = (
                    prompt_details.get("cached_tokens", 0)
                    if isinstance(prompt_details, dict)
                    else 0
                )
            reasoning = (
                completion_details.get("reasoning_tokens", 0)
                if isinstance(completion_details, dict)
                else 0
            )
            assert isinstance(cached, int) and not isinstance(cached, bool)
            assert isinstance(reasoning, int) and not isinstance(reasoning, bool)
            return ProviderUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached,
                reasoning_tokens=reasoning,
                complete=True,
            )
        except (AssertionError, KeyError, TypeError, ValueError):
            return ProviderUsage.incomplete()


__all__ = [
    "OpenAICompatibleChatAdapter",
    "OpenAICompatibleSettings",
]
