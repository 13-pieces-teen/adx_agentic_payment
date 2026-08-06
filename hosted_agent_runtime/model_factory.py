"""Allowlisted PydanticAI model composition for Hosted Arena Agents."""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionToolChoiceOptionParam,
    ChatCompletionToolParam,
)
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import (
    OpenAIChatModel,
    OpenAIChatModelSettings,
)
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from .capabilities import CapabilityRegistry, ResolvedModelCapability
from .providers.openai_compatible import OpenAICompatibleSettings


class _DeepSeekOpenAIChatModel(OpenAIChatModel):
    """Apply the official DeepSeek V4 thinking/tool-call wire contract."""

    def _get_tool_choice(
        self,
        model_settings: OpenAIChatModelSettings,
        model_request_parameters: ModelRequestParameters,
    ) -> tuple[
        list[ChatCompletionToolParam],
        ChatCompletionToolChoiceOptionParam | None,
    ]:
        tools, _ = super()._get_tool_choice(
            model_settings,
            model_request_parameters,
        )
        # DeepSeek V4 thinking mode rejects the tool_choice parameter,
        # including "auto". Filtering performed by the parent still preserves
        # PydanticAI's output-retry narrowing when only one tool is legal.
        return tools, None

    def _map_model_response(
        self,
        message: ModelResponse,
    ) -> ChatCompletionMessageParam | None:
        mapped = super()._map_model_response(message)
        if (
            mapped is not None
            and mapped["role"] == "assistant"
            and mapped.get("tool_calls")
            and mapped.get("content") is None
        ):
            # DeepSeek V4 requires non-null assistant content on tool turns.
            mapped["content"] = ""
        return mapped


@dataclass(slots=True)
class BuiltPydanticModel:
    model: OpenAIChatModel
    settings: OpenAIChatModelSettings
    resolved: ResolvedModelCapability
    _client: AsyncOpenAI

    async def close(self) -> None:
        await self._client.close()


class PydanticModelFactory:
    """Build models only for deployment-owned endpoints and capabilities."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def build(
        self,
        *,
        provider_id: str,
        model_id: str,
        api_key: str,
        thinking_enabled: bool,
        remaining_timeout_ms: int,
        requested_max_output_tokens: int,
    ) -> BuiltPydanticModel:
        if not api_key:
            raise ValueError("Hosted model credential is empty")
        resolved = self._registry.resolve(
            provider_id=provider_id,
            model_id=model_id,
            thinking_enabled=thinking_enabled,
            remaining_timeout_ms=remaining_timeout_ms,
            requested_max_output_tokens=requested_max_output_tokens,
        )
        base_url, thinking_dialect = self._trusted_provider(
            provider_id
        )
        timeout_seconds = max(0.001, resolved.request_timeout_ms / 1000)
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout_seconds,
                connect=min(10.0, timeout_seconds),
            ),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=8,
                max_keepalive_connections=4,
            ),
        )
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
            http_client=http_client,
        )
        extra_body: object | None = None
        if thinking_dialect == "deepseek":
            extra_body = {
                "thinking": {
                    "type": (
                        "enabled"
                        if resolved.thinking_enabled
                        else "disabled"
                    )
                }
            }
        settings = OpenAIChatModelSettings(
            max_tokens=resolved.max_output_tokens,
            timeout=timeout_seconds,
            parallel_tool_calls=False,
            extra_body=extra_body,
        )
        return BuiltPydanticModel(
            model=(
                _DeepSeekOpenAIChatModel(
                    resolved.model_id,
                    provider=OpenAIProvider(openai_client=client),
                    profile=self._deepseek_profile(),
                )
                if thinking_dialect == "deepseek"
                else OpenAIChatModel(
                    resolved.model_id,
                    provider=OpenAIProvider(openai_client=client),
                )
            ),
            settings=settings,
            resolved=resolved,
            _client=client,
        )

    @staticmethod
    def _trusted_provider(provider_id: str) -> tuple[str, str]:
        if provider_id == "deepseek":
            OpenAICompatibleSettings(
                adapter_id="deepseek-pydantic-chat-v1",
                endpoint="https://api.deepseek.com/chat/completions",
                response_format_mode="json_object",
                thinking_dialect="deepseek",
            )
            return "https://api.deepseek.com", "deepseek"
        if provider_id == "official-deepseek":
            OpenAICompatibleSettings(
                adapter_id="official-deepseek-pydantic-chat-v1",
                endpoint=(
                    "http://official-litellm:4000/v1/chat/completions"
                ),
                response_format_mode="json_object",
                thinking_dialect="deepseek",
                allow_http_hostname="official-litellm",
            )
            return "http://official-litellm:4000/v1", "deepseek"
        if provider_id == "openai-compatible":
            endpoint = os.getenv(
                "ADX_OPENAI_COMPATIBLE_ENDPOINT", ""
            ).strip()
            settings = OpenAICompatibleSettings(
                adapter_id="deployment-pydantic-chat-v1",
                endpoint=endpoint,
                response_format_mode="json_object",
                thinking_dialect="none",
            )
            suffix = "/chat/completions"
            if not settings.endpoint.endswith(suffix):
                raise ValueError(
                    "OpenAI-compatible endpoint must end in /chat/completions"
                )
            return settings.endpoint[: -len(suffix)], "none"
        raise ValueError("Hosted provider has no PydanticAI model factory")

    @staticmethod
    def _deepseek_profile() -> OpenAIModelProfile:
        """Describe DeepSeek V4's thinking/tool-call wire compatibility."""

        return OpenAIModelProfile(
            supports_thinking=True,
            openai_chat_thinking_field="reasoning_content",
            openai_chat_send_back_thinking_parts="field",
            # DeepSeek's OpenAI-compatible endpoint accepts `max_tokens`.
            # PydanticAI otherwise defaults this setting to OpenAI's
            # `max_completion_tokens`, which DeepSeek/LiteLLM may ignore.
            openai_chat_supports_max_completion_tokens=False,
            openai_supports_tool_choice_required=False,
            openai_system_prompt_role="system",
        )


__all__ = ["BuiltPydanticModel", "PydanticModelFactory"]
