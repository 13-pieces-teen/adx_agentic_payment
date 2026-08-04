"""Deterministic Arena projection for one authoritative terminal result."""

from __future__ import annotations

from arena_agent_contracts import (
    AcceptAction,
    AgentTaskResultV1,
    ArenaAgentTaskV1,
    ArenaDecideInputV1,
    ArenaMarketIntentInputV1,
    ArenaMarketRfqInputV1,
    ArenaMarketSelectInputV1,
    ArenaNegotiateInputV1,
    BuyAction,
    PassAction,
    ProposeAction,
    RejectAction,
    RequestNegotiationsActionV1,
    SellAction,
    EngageRequestActionV1,
    RejectAllRequestsActionV1,
)

from .candidate_validation import (
    decide_candidate_violation,
    market_intent_candidate_violation,
    market_rfq_candidate_violation,
    market_select_candidate_violation,
    negotiation_candidate_violation,
)
from .models import ArenaApplication


def _default_application(
    task: ArenaAgentTaskV1, *, reason: str
) -> ArenaApplication:
    if task.kind in {"arena.decide", "arena.market.intent"}:
        return ArenaApplication(
            accepted=True,
            outcome="default_pass",
            action={"action": "pass"},
            rejection_reason=reason,
        )
    outcome = (
        "negotiation_timeout"
        if task.kind == "arena.negotiate"
        else "market_timeout"
    )
    return ArenaApplication(
        accepted=True,
        outcome=outcome,
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

    if (
        task.kind == "arena.market.intent"
        and isinstance(task.input, ArenaMarketIntentInputV1)
        and isinstance(action, (BuyAction, SellAction, PassAction))
    ):
        violation = market_intent_candidate_violation(task.input, action)
        if violation is not None:
            return _default_application(task, reason=violation)
        return ArenaApplication(
            accepted=True,
            outcome="candidate",
            action=action.model_dump(mode="json", by_alias=True),
        )

    if (
        task.kind == "arena.market.rfq"
        and isinstance(task.input, ArenaMarketRfqInputV1)
        and isinstance(action, (RequestNegotiationsActionV1, PassAction))
    ):
        violation = market_rfq_candidate_violation(task.input, action)
        if violation is not None:
            return _default_application(task, reason=violation)
        return ArenaApplication(
            accepted=True,
            outcome="candidate",
            action=action.model_dump(mode="json", by_alias=True),
        )

    if (
        task.kind == "arena.market.select"
        and isinstance(task.input, ArenaMarketSelectInputV1)
        and isinstance(action, (EngageRequestActionV1, RejectAllRequestsActionV1))
    ):
        violation = market_select_candidate_violation(task.input, action)
        if violation is not None:
            return _default_application(task, reason=violation)
        return ArenaApplication(
            accepted=True,
            outcome="candidate",
            action=action.model_dump(mode="json", by_alias=True),
        )

    return _default_application(task, reason="action_kind_mismatch")


__all__ = ["derive_application"]
