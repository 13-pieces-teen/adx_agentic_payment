from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition

from hosted_agent_runtime.capabilities import CapabilityError
from hosted_agent_runtime.model_factory import (
    PydanticModelFactory,
    _DeepSeekOpenAIChatModel,
)
from hosted_agent_runtime.production_providers import (
    build_production_capability_registry,
)


def test_factory_builds_only_allowlisted_official_model() -> None:
    factory = PydanticModelFactory(
        build_production_capability_registry(include_official=True)
    )
    built = factory.build(
        provider_id="official-deepseek",
        model_id="deepseek-v4-flash",
        api_key="test-only-key",
        thinking_enabled=False,
        remaining_timeout_ms=15_000,
        requested_max_output_tokens=2_048,
    )
    try:
        assert built.resolved.provider_id == "official-deepseek"
        assert built.resolved.model_id == "deepseek-v4-flash"
        assert built.resolved.thinking_enabled is False
        assert built.resolved.request_timeout_ms == 15_000
        assert built.resolved.max_output_tokens == 2_048
        assert built.settings["parallel_tool_calls"] is False
        assert built.settings["extra_body"] == {
            "thinking": {"type": "disabled"}
        }
        assert (
            built.model.profile["openai_supports_tool_choice_required"]
            is True
        )
        assert (
            built.model.profile["openai_chat_thinking_field"]
            == "reasoning_content"
        )
        assert (
            built.model.profile["openai_chat_send_back_thinking_parts"]
            == "field"
        )
        assert (
            built.model.profile[
                "openai_chat_supports_max_completion_tokens"
            ]
            is False
        )
        assert built.model.profile["openai_system_prompt_role"] == "system"
        assert (
            built.model.profile["default_structured_output_mode"]
            == "prompted"
        )
        assert (
            built.model.profile["supports_json_object_output"]
            is True
        )
        assert isinstance(built.model, _DeepSeekOpenAIChatModel)
        tools, tool_choice = built.model._get_tool_choice(
            built.settings,
            ModelRequestParameters(
                function_tools=[
                    ToolDefinition(
                        name="inspect_portfolio",
                        parameters_json_schema={
                            "type": "object",
                            "properties": {},
                        },
                    )
                ],
                output_mode="prompted",
                allow_text_output=True,
            ),
        )
        assert len(tools) == 1
        assert tool_choice == "required"
        mapped = built.model._map_model_response(
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="inspect_portfolio",
                        args={},
                        tool_call_id="call-test",
                    )
                ]
            )
        )
        assert mapped is not None
        assert mapped["content"] == ""
    finally:
        asyncio.run(built.close())
        asyncio.run(factory.close())


def test_factory_reuses_sanitized_transport_until_factory_close() -> None:
    factory = PydanticModelFactory(
        build_production_capability_registry(include_official=True)
    )
    first = factory.build(
        provider_id="official-deepseek",
        model_id="deepseek-v4-flash",
        api_key="first-task-key",
        thinking_enabled=False,
        remaining_timeout_ms=15_000,
        requested_max_output_tokens=256,
    )
    second = factory.build(
        provider_id="official-deepseek",
        model_id="deepseek-v4-flash",
        api_key="second-task-key",
        thinking_enabled=False,
        remaining_timeout_ms=15_000,
        requested_max_output_tokens=256,
    )

    transport = first._client._client
    assert second._client._client is transport
    assert transport.headers.get("Authorization") is None

    asyncio.run(first.close())
    assert first._client.api_key == ""
    assert transport.is_closed is False
    assert second._client.api_key == "second-task-key"

    asyncio.run(second.close())
    assert second._client.api_key == ""
    assert transport.is_closed is False

    asyncio.run(factory.close())
    assert transport.is_closed is True


def test_factory_fails_closed_for_unregistered_provider() -> None:
    factory = PydanticModelFactory(
        build_production_capability_registry(include_official=True)
    )

    try:
        with pytest.raises((CapabilityError, ValueError)):
            factory.build(
                provider_id="attacker-endpoint",
                model_id="deepseek-v4-flash",
                api_key="test-only-key",
                thinking_enabled=False,
                remaining_timeout_ms=15_000,
                requested_max_output_tokens=256,
            )
    finally:
        asyncio.run(factory.close())
