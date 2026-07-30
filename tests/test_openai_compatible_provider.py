from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from hosted_agent_runtime.providers import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleSettings,
    ProviderInvocationError,
    ProviderRequest,
)
from hosted_agent_runtime.secret_store import WorkerSecret


def _request(*, thinking: bool = True) -> ProviderRequest:
    return ProviderRequest(
        attempt_id="attempt-1",
        task_id="task-1",
        task_kind="arena.decide",
        idempotency_key="game:round:agent:decide",
        model_id="deepseek-v4-flash",
        prompt_version="arena.hosted-prompt.v1",
        context_version="arena.agent-task.v1",
        output_version="arena.agent-action.v1",
        system_instructions="Return JSON.",
        input_json='{"round":1}',
        output_schema_json='{"type":"object"}',
        thinking_enabled=thinking,
        thinking_parameter_name="thinking.enabled",
        max_output_tokens=512,
        request_timeout_ms=5_000,
    )


def test_deepseek_adapter_sends_fixed_endpoint_and_discards_reasoning() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["body"] = json.loads(request.content)
        observed["authorization"] = request.headers["authorization"]
        return httpx.Response(
            200,
            headers={"x-request-id": "req-safe-1"},
            json={
                "id": "ignored",
                "model": "deepseek-v4-flash-20260724",
                "choices": [
                    {
                        "message": {
                            "content": '{"action":"pass"}',
                            "reasoning_content": "must never cross adapter",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "prompt_cache_hit_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 2},
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            },
        )

    async def run() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
        )
        adapter = OpenAICompatibleChatAdapter(
            OpenAICompatibleSettings(
                adapter_id="deepseek-openai-chat-v1",
                endpoint="https://api.deepseek.com/chat/completions",
                thinking_dialect="deepseek",
            ),
            client=client,
        )
        with WorkerSecret(b"test-key") as key:
            response = await adapter.invoke(_request(), key)
        await client.aclose()
        assert dict(response.structured_output) == {"action": "pass"}
        assert response.actual_model == "deepseek-v4-flash-20260724"
        assert response.usage.cached_input_tokens == 4
        assert response.usage.reasoning_tokens == 3
        assert not hasattr(response, "reasoning_content")

    asyncio.run(run())
    assert observed["url"] == "https://api.deepseek.com/chat/completions"
    assert observed["authorization"] == "Bearer test-key"
    body = observed["body"]
    assert isinstance(body, dict)
    assert body["thinking"] == {"type": "enabled"}
    assert body["response_format"] == {"type": "json_object"}
    messages = body["messages"]
    assert isinstance(messages, list)
    assert '"type":"object"' in messages[0]["content"]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "authentication_failed"),
        (403, "authentication_failed"),
        (429, "rate_limited"),
        (500, "provider_unavailable"),
        (400, "permanent_request"),
    ],
)
def test_http_failures_are_reduced_to_safe_codes(
    status: int,
    code: str,
) -> None:
    async def run() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(status, text="secret provider body")
            )
        )
        adapter = OpenAICompatibleChatAdapter(
            OpenAICompatibleSettings(
                adapter_id="test",
                endpoint="https://provider.example/v1/chat/completions",
            ),
            client=client,
        )
        with WorkerSecret(b"test-key") as key:
            with pytest.raises(ProviderInvocationError) as exc:
                await adapter.invoke(_request(), key)
        await client.aclose()
        assert exc.value.code == code
        assert "secret provider body" not in str(exc.value)

    asyncio.run(run())


def test_response_body_is_bounded_while_streaming() -> None:
    async def run() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    content=b"x" * (1_048_576 + 1),
                )
            )
        )
        adapter = OpenAICompatibleChatAdapter(
            OpenAICompatibleSettings(
                adapter_id="test",
                endpoint="https://provider.example/v1/chat/completions",
            ),
            client=client,
        )
        with WorkerSecret(b"test-key") as key:
            with pytest.raises(ProviderInvocationError) as exc:
                await adapter.invoke(_request(), key)
        await client.aclose()
        assert exc.value.code == "invalid_json"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("endpoint", "allow_http_hostname"),
    [
        (
            "http://provider.example/v1/chat/completions",
            None,
        ),
        (
            "http://official-litellm.evil:4000/v1/chat/completions",
            "official-litellm",
        ),
        (
            "http://another-service:4000/v1/chat/completions",
            "official-litellm",
        ),
    ],
)
def test_http_endpoint_requires_an_exact_allowed_hostname(
    endpoint: str,
    allow_http_hostname: str | None,
) -> None:
    with pytest.raises(
        ValueError,
        match="invalid OpenAI-compatible provider settings",
    ):
        OpenAICompatibleSettings(
            adapter_id="test",
            endpoint=endpoint,
            allow_http_hostname=allow_http_hostname,
        )


def test_exact_internal_http_hostname_is_allowed() -> None:
    settings = OpenAICompatibleSettings(
        adapter_id="official-deepseek-litellm-chat-v1",
        endpoint="http://official-litellm:4000/v1/chat/completions",
        allow_http_hostname="official-litellm",
    )

    assert settings.endpoint == (
        "http://official-litellm:4000/v1/chat/completions"
    )
    assert settings.allow_http_hostname == "official-litellm"
