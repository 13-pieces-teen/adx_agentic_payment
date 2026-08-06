from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic_ai import models
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from arena_agent_contracts import (
    AGENT_TASK_SCHEMA_VERSION_V1,
    ArenaAgentTaskV1,
)
from arena_core.hashing import sha256_identifier
from hosted_agent_runtime.context import HostedArenaAgentContext
from hosted_agent_runtime.memory import HostedGameMemory
from hosted_agent_runtime.runtime import (
    HostedAgentRuntimeLimits,
    HostedArenaAgentRuntime,
)
from hosted_agent_runtime.strategy import StrategyArchetype
from tests.arena_core_helpers import decide_input


pytestmark = pytest.mark.anyio
models.ALLOW_MODEL_REQUESTS = False


def test_runtime_default_budget_covers_multi_request_agent_run() -> None:
    limits = HostedAgentRuntimeLimits()

    assert limits.request_limit == 4
    assert limits.tool_calls_limit == 6
    assert limits.output_tokens_limit == 65_536


def _task() -> ArenaAgentTaskV1:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=10)
    participant = decide_input(deadline=deadline)
    return ArenaAgentTaskV1(
        task_id="task-pydantic-agent-1",
        kind="arena.decide",
        schema_version=AGENT_TASK_SCHEMA_VERSION_V1,
        game_id=participant.game_id,
        round_id=participant.round_id,
        game_agent_id="game-agent-1",
        deadline_at=deadline,
        idempotency_key=(
            f"{participant.game_id}:{participant.round_id}:"
            "game-agent-1:decide"
        ),
        input_hash=sha256_identifier(participant),
        input=participant,
    )


def _context(task: ArenaAgentTaskV1) -> HostedArenaAgentContext:
    return HostedArenaAgentContext(
        task=task,
        agent_id="agent-hosted-1",
        strategy_revision_id="strategy:test-1",
        strategy_revision_no=1,
        strategy_archetype=StrategyArchetype.BALANCED,
        strategy_catalog_version="arena.hosted-strategy.v1",
        strategy_instructions=(
            "Balance expected value and liquidity while obeying hard limits."
        ),
        game_memory=HostedGameMemory(
            memory_version=2,
            state={
                "gameAgentId": task.game_agent_id,
                "latestPlan": "Preserve enough cash for the final round.",
            },
        ),
    )


def _output(action: dict[str, object]) -> dict[str, object]:
    return {
        "action": action,
        "decision_summary": {
            "plan": "Preserve optionality while waiting for a clearer edge.",
            "factors": ["No sufficiently strong price dislocation."],
            "confidence_bps": 7200,
        },
        "memory_patch": {
            "round_summary": "Reviewed every currently allowed market action.",
            "next_plan": "Re-evaluate after the next public event.",
            "observations": ["Current prices do not justify extra exposure."],
            "strategy_adjustments": [],
            "risk_budget_bps": 4200,
        },
    }


async def test_pydantic_runtime_uses_tools_and_returns_typed_action() -> None:
    task = _task()
    runtime = HostedArenaAgentRuntime(
        model=TestModel(custom_output_args=_output({"action": "pass"})),
        context=_context(task),
        actual_model="test",
    )

    execution = await runtime.execute_with_metadata(task, task.deadline_at)

    assert execution.result.status == "succeeded"
    assert execution.result.action is not None
    assert execution.result.action.action == "pass"
    assert execution.agent_output is not None
    assert execution.request_count >= 2
    assert execution.tool_call_count >= 1
    assert execution.usage.complete is True


async def test_pydantic_runtime_rejects_task_incompatible_candidate() -> None:
    task = _task()
    runtime = HostedArenaAgentRuntime(
        model=TestModel(
            custom_output_args=_output(
                {
                    "action": "buy",
                    "good": "iron",
                    "limitPrice": "1.000000",
                }
            )
        ),
        context=_context(task),
    )

    execution = await runtime.execute_with_metadata(task, task.deadline_at)

    assert execution.result.status == "failed"
    assert execution.result.action is None
    assert execution.error_code == "invalid_structured_output"


async def test_pydantic_runtime_fails_closed_after_deadline() -> None:
    task = _task()
    runtime = HostedArenaAgentRuntime(
        model=TestModel(custom_output_args=_output({"action": "pass"})),
        context=_context(task),
    )

    result = await runtime.execute(
        task,
        datetime.now(timezone.utc) - timedelta(milliseconds=1),
    )

    assert result.status == "timed_out"
    assert result.action is None


async def test_request_limit_exhaustion_is_retryable_and_keeps_usage() -> None:
    class _RequestLimitAgent:
        async def run(self, *_args, **kwargs):
            run_usage = kwargs["usage"]
            run_usage.incr(
                RunUsage(
                    requests=4,
                    tool_calls=3,
                    input_tokens=1_200,
                    output_tokens=480,
                )
            )
            raise UsageLimitExceeded(
                "The next request would exceed the request_limit of 4"
            )

    task = _task()
    runtime = HostedArenaAgentRuntime(
        model=TestModel(custom_output_args=_output({"action": "pass"})),
        context=_context(task),
    )
    runtime._agent = _RequestLimitAgent()

    execution = await runtime.execute_with_metadata(task, task.deadline_at)

    assert execution.result.status == "failed"
    assert execution.error_code == "invalid_structured_output"
    assert execution.request_count == 4
    assert execution.tool_call_count == 3
    assert execution.usage.input_tokens == 1_200
    assert execution.usage.output_tokens == 480
    assert execution.usage.complete is True


def test_non_request_usage_limit_remains_permanent() -> None:
    error = UsageLimitExceeded(
        "Exceeded the output_tokens_limit of 8192 (output_tokens=9000)"
    )

    assert (
        HostedArenaAgentRuntime._usage_limit_error_code(error)
        == "permanent_request"
    )
