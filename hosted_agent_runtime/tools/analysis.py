"""Deterministic tools that read only the frozen PydanticAI dependencies."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from pydantic_ai import RunContext

from arena_agent_contracts import (
    ArenaDecideInputV1,
    ArenaMarketIntentInputV1,
    ArenaNegotiateInputV1,
)

from ..context import HostedArenaAgentContext


def _record_analysis_tool(
    ctx: RunContext[HostedArenaAgentContext],
    tool_name: str,
) -> None:
    ctx.deps.analysis_tool_calls.add(tool_name)


def inspect_portfolio(
    ctx: RunContext[HostedArenaAgentContext],
) -> dict[str, object]:
    """Return the frozen cash, inventory, and marked portfolio inputs."""

    _record_analysis_tool(ctx, "inspect_portfolio")
    task_input = ctx.deps.task.input
    value: dict[str, object] = {
        "taskKind": ctx.deps.task.kind,
        "roundIndex": task_input.round_index,
    }
    if hasattr(task_input, "cash"):
        value["cash"] = format(task_input.cash, "f")
    if isinstance(task_input, (ArenaDecideInputV1, ArenaMarketIntentInputV1)):
        value["holdings"] = dict(task_input.holdings)
        value["market"] = {
            good: format(price, "f")
            for good, price in task_input.market.items()
        }
        value["eventImpliedFinal"] = {
            good: format(price, "f")
            for good, price in task_input.event_implied_final.items()
        }
        value["allowedActions"] = list(task_input.limits.allowed_actions)
        value["allowedGoods"] = list(task_input.limits.allowed_goods)
    return value


def inspect_market_history(
    ctx: RunContext[HostedArenaAgentContext],
) -> dict[str, object]:
    """Return bounded public history already frozen into this AgentTask."""

    _record_analysis_tool(ctx, "inspect_market_history")
    task_input = ctx.deps.task.input
    value: dict[str, object] = {
        "events": [
            event.model_dump(mode="json", by_alias=True)
            for event in task_input.events
        ],
    }
    if isinstance(task_input, (ArenaDecideInputV1, ArenaMarketIntentInputV1)):
        value["completedActions"] = [
            item.model_dump(mode="json", by_alias=True)
            for item in task_input.completed_actions
        ]
        value["completedTrades"] = [
            item.model_dump(mode="json", by_alias=True)
            for item in task_input.completed_trades
        ]
        value["marketActivity"] = [
            item.model_dump(mode="json", by_alias=True)
            for item in task_input.market_activity
        ]
    elif isinstance(task_input, ArenaNegotiateInputV1):
        value["history"] = [
            item.model_dump(mode="json", by_alias=True)
            for item in task_input.history
        ]
    return value


def recall_strategy_and_plan(
    ctx: RunContext[HostedArenaAgentContext],
) -> dict[str, object]:
    """Return the frozen strategy revision and last applied private memory."""

    _record_analysis_tool(ctx, "recall_strategy_and_plan")
    return {
        "strategyRevisionId": ctx.deps.strategy_revision_id,
        "strategyRevisionNo": ctx.deps.strategy_revision_no,
        "strategyArchetype": ctx.deps.strategy_archetype.value,
        "strategyCatalogVersion": ctx.deps.strategy_catalog_version,
        "strategyInstructions": ctx.deps.strategy_instructions,
        "memoryVersion": ctx.deps.game_memory.memory_version,
        "memoryState": ctx.deps.game_memory.state,
    }


def evaluate_negotiation_boundary(
    ctx: RunContext[HostedArenaAgentContext],
    proposed_price: str,
) -> dict[str, object]:
    """Check a proposed price against the frozen negotiation hard limit."""

    _record_analysis_tool(ctx, "evaluate_negotiation_boundary")
    task_input = ctx.deps.task.input
    if not isinstance(task_input, ArenaNegotiateInputV1):
        return {"applicable": False, "reason": "not_negotiation_task"}
    try:
        price = Decimal(proposed_price)
    except (InvalidOperation, ValueError):
        return {"applicable": True, "legal": False, "reason": "invalid_price"}
    if not price.is_finite() or price <= 0:
        return {"applicable": True, "legal": False, "reason": "invalid_price"}
    limit_price = task_input.limit_price
    if limit_price is None:
        return {"applicable": True, "legal": True, "hardLimit": None}
    legal = (
        price <= limit_price
        if task_input.role == "buyer"
        else price >= limit_price
    )
    return {
        "applicable": True,
        "legal": legal,
        "role": task_input.role,
        "hardLimit": format(limit_price, "f"),
    }
