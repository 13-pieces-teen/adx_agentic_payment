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

from .candidate_validation import (
    decide_candidate_violation,
    negotiation_candidate_violation,
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
        violation = decide_candidate_violation(task.input, action)
        if violation is not None:
            return _default_application(task, reason=violation)
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
        violation = negotiation_candidate_violation(task.input, action)
        if violation is not None:
            return _default_application(task, reason=violation)
        return ArenaApplication(
            accepted=True,
            outcome="candidate",
            action=action.model_dump(mode="json", by_alias=True),
        )

    return _default_application(task, reason="action_kind_mismatch")


__all__ = ["derive_application"]
