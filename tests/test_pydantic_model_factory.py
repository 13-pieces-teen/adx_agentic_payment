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
        thinking_enabled=True,
        remaining_timeout_ms=15_000,
        requested_max_output_tokens=2_048,
    )
    try:
        assert built.resolved.provider_id == "official-deepseek"
        assert built.resolved.model_id == "deepseek-v4-flash"
        assert built.resolved.thinking_enabled is True
        assert built.resolved.request_timeout_ms == 15_000
        assert built.resolved.max_output_tokens == 2_048
        assert built.settings["parallel_tool_calls"] is False
        assert built.settings["extra_body"] == {
            "thinking": {"type": "enabled"}
        }
        assert (
            built.model.profile["openai_supports_tool_choice_required"]
            is False
        )
        assert (
            built.model.profile["openai_chat_thinking_field"]
            == "reasoning_content"
        )
        assert (
            built.model.profile["openai_chat_send_back_thinking_parts"]
            == "field"
        )
        assert built.model.profile["openai_system_prompt_role"] == "system"
        assert isinstance(built.model, _DeepSeekOpenAIChatModel)
        tools, tool_choice = built.model._get_tool_choice(
            {},
            ModelRequestParameters(
                function_tools=[
                    ToolDefinition(
                        name="inspect_portfolio",
                        parameters_json_schema={
                            "type": "object",
                            "properties": {},
                        },
                    )
                ]
            ),
        )
        assert len(tools) == 1
        assert tool_choice is None
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


def test_factory_fails_closed_for_unregistered_provider() -> None:
    factory = PydanticModelFactory(
        build_production_capability_registry(include_official=True)
    )

    with pytest.raises((CapabilityError, ValueError)):
        factory.build(
            provider_id="attacker-endpoint",
            model_id="deepseek-v4-flash",
            api_key="test-only-key",
            thinking_enabled=False,
            remaining_timeout_ms=15_000,
            requested_max_output_tokens=256,
        )
