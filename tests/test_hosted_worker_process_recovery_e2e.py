from __future__ import annotations

import json

from tests.hosted_worker_process_recovery_e2e import (
    _chat_completion,
    _parser,
    _worker_environment,
)


def test_fake_provider_supports_validation_and_bounded_agent_tool_sequence() -> None:
    validation, is_validation = _chat_completion({"messages": []})
    assert is_validation is True
    assert json.loads(
        validation["choices"][0]["message"]["content"]  # type: ignore[index]
    ) == {"ok": True}

    tools = [
        {
            "type": "function",
            "function": {
                "name": "recall_strategy_and_plan",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "final_result",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {},
                        "decision_summary": {},
                        "memory_patch": {},
                    },
                },
            },
        },
    ]
    tool_request, is_validation = _chat_completion(
        {"messages": [{"role": "user"}], "tools": tools}
    )
    assert is_validation is False
    tool_call = tool_request["choices"][0]["message"]["tool_calls"][0]  # type: ignore[index]
    assert tool_call["function"]["name"] == "recall_strategy_and_plan"
    assert json.loads(tool_call["function"]["arguments"]) == {}

    output_request, is_validation = _chat_completion(
        {
            "messages": [
                {"role": "user"},
                {"role": "tool", "content": "{}"},
            ],
            "tools": tools,
        }
    )
    assert is_validation is False
    output_call = output_request["choices"][0]["message"]["tool_calls"][0]  # type: ignore[index]
    assert output_call["function"]["name"] == "final_result"
    output = json.loads(output_call["function"]["arguments"])
    assert output["action"] == {"action": "pass"}
    assert output["memory_patch"]["risk_budget_bps"] == 4000


def test_recovery_harness_defaults_to_production_compatible_worker_lease() -> None:
    environment = _worker_environment(
        worker_url="postgresql://worker@example.invalid/arena",
        worker_id="worker-1",
    )
    assert "ADX_HOSTED_WORKER_LEASE_SECONDS=30" in environment
    assert "ADX_HOSTED_WORKER_TASK_CONCURRENCY=1" in environment

    parsed = _parser().parse_args(["run", "--keep"])
    assert parsed.command == "run"
    assert parsed.keep is True
