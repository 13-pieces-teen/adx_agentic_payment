"""Deterministic Arena projection for one authoritative terminal result."""

from __future__ import annotations

from arena_agent_contracts import (
    AcceptAction,
    AgentTaskResultV1,
    ArenaAgentTaskV1,
    ArenaDecideInputV1,
    ArenaNegotiateInputV1,
    BuyAction,
    PassAction,
    ProposeAction,
    RejectAction,
    SellAction,
)

from .models import ArenaApplication


def _default_application(
    task: ArenaAgentTaskV1, *, reason: str
) -> ArenaApplication:
    if task.kind == "arena.decide":
        return ArenaApplication(
            accepted=True,
            outcome="default_pass",
            action={"action": "pass"},
            rejection_reason=reason,
        )
    return ArenaApplication(
        accepted=True,
        outcome="negotiation_timeout",
        action=None,
        rejection_reason=reason,
    )


def derive_application(
    task: ArenaAgentTaskV1, result: AgentTaskResultV1
) -> ArenaApplication:
    """Map a persisted Runtime result to exactly one deterministic outcome.

    This is deliberately derived again inside the repository CAS boundary.
    Callers cannot supply an arbitrary action projection.
    """

    if result.status != "succeeded":
        return _default_application(task, reason=f"runtime_{result.status}")

    action = result.action
    if (
        task.kind == "arena.decide"
        and isinstance(task.input, ArenaDecideInputV1)
        and isinstance(action, (BuyAction, SellAction, PassAction))
    ):
        if isinstance(action, (BuyAction, SellAction)):
            limits = task.input.limits
            if action.action not in limits.allowed_actions:
                return _default_application(
                    task, reason="action_not_allowed"
                )
            if limits.allowed_goods and action.good not in limits.allowed_goods:
                return _default_application(
                    task, reason="good_not_allowed"
                )
        elif action.action not in task.input.limits.allowed_actions:
            return _default_application(task, reason="action_not_allowed")
        return ArenaApplication(
            accepted=True,
            outcome="candidate",
            action=action.model_dump(mode="json", by_alias=True),
        )

    if (
        task.kind == "arena.negotiate"
        and isinstance(task.input, ArenaNegotiateInputV1)
        and isinstance(action, (ProposeAction, AcceptAction, RejectAction))
    ):
        if task.input.turn_sequence == 1 and not isinstance(
            action, ProposeAction
        ):
            return _default_application(
                task,
                reason="buyer_opening_proposal_required",
            )
        if isinstance(action, AcceptAction) and (
            task.input.latest_counterparty_quote is None
        ):
            return _default_application(
                task,
                reason="counterparty_proposal_required",
            )
        if task.input.remaining_turns == 0 and isinstance(
            action, ProposeAction
        ):
            return _default_application(
                task,
                reason="final_turn_must_close",
            )
        return ArenaApplication(
            accepted=True,
            outcome="candidate",
            action=action.model_dump(mode="json", by_alias=True),
        )

    return _default_application(task, reason="action_kind_mismatch")


__all__ = ["derive_application"]
