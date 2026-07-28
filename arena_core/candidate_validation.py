"""Shared deterministic validation for Runtime candidates and Arena apply.

The Hosted Runtime uses these checks only to spend its one bounded correction
attempt more usefully. Arena remains authoritative and repeats the same checks
when it projects a persisted result.
"""

from __future__ import annotations

from typing import Literal, TypeAlias

from arena_agent_contracts import (
    AcceptAction,
    ArenaDecideInputV1,
    ArenaNegotiateInputV1,
    BuyAction,
    DecideActionV1,
    NegotiateActionV1,
    PassAction,
    ProposeAction,
    RejectAction,
    SellAction,
)


DecideCandidateViolation: TypeAlias = Literal[
    "action_not_allowed",
    "good_not_allowed",
    "insufficient_inventory",
    "insufficient_cash",
]
NegotiationCandidateViolation: TypeAlias = Literal[
    "buyer_opening_proposal_required",
    "counterparty_proposal_required",
    "final_turn_must_close",
    "limit_price_violation",
    "in_bound_quote_must_accept",
    "final_out_of_bound_quote_must_reject",
    "out_of_bound_quote_must_counter",
    "counter_must_equal_limit",
]
CandidateViolation: TypeAlias = (
    DecideCandidateViolation | NegotiationCandidateViolation
)


def decide_candidate_violation(
    task_input: ArenaDecideInputV1,
    action: DecideActionV1,
) -> DecideCandidateViolation | None:
    """Return the first deterministic frozen-portfolio violation, if any."""

    limits = task_input.limits
    if action.action not in limits.allowed_actions:
        return "action_not_allowed"
    if isinstance(action, PassAction):
        return None
    if not isinstance(action, (BuyAction, SellAction)):
        raise TypeError("action must be a decide action")
    if limits.allowed_goods and action.good not in limits.allowed_goods:
        return "good_not_allowed"
    if (
        isinstance(action, SellAction)
        and task_input.holdings.get(action.good, 0) < action.quantity
    ):
        return "insufficient_inventory"
    if (
        isinstance(action, BuyAction)
        and action.limit_price is not None
        and action.limit_price * action.quantity > task_input.cash
    ):
        return "insufficient_cash"
    return None


def _quote_is_within_boundary(task_input: ArenaNegotiateInputV1) -> bool:
    quote = task_input.latest_counterparty_quote
    if quote is None:
        raise ValueError("counterparty quote is required")
    if task_input.limit_price is None:
        return True
    if task_input.role == "buyer":
        return quote.price <= task_input.limit_price
    return quote.price >= task_input.limit_price


def negotiation_candidate_violation(
    task_input: ArenaNegotiateInputV1,
    action: NegotiateActionV1,
) -> NegotiationCandidateViolation | None:
    """Validate hard boundary and deterministic convergence semantics."""

    if task_input.turn_sequence == 1 and not isinstance(
        action, ProposeAction
    ):
        return "buyer_opening_proposal_required"
    if (
        isinstance(action, AcceptAction)
        and task_input.latest_counterparty_quote is None
    ):
        return "counterparty_proposal_required"
    if task_input.remaining_turns <= 1 and isinstance(action, ProposeAction):
        return "final_turn_must_close"

    limit_price = task_input.limit_price
    if isinstance(action, ProposeAction) and limit_price is not None:
        if (
            task_input.role == "buyer" and action.price > limit_price
        ) or (
            task_input.role == "seller" and action.price < limit_price
        ):
            return "limit_price_violation"
    if (
        isinstance(action, AcceptAction)
        and task_input.latest_counterparty_quote is not None
        and limit_price is not None
    ):
        quote_price = task_input.latest_counterparty_quote.price
        if (
            task_input.role == "buyer" and quote_price > limit_price
        ) or (
            task_input.role == "seller" and quote_price < limit_price
        ):
            return "limit_price_violation"

    quote = task_input.latest_counterparty_quote
    if quote is None:
        return None

    if _quote_is_within_boundary(task_input):
        if not isinstance(action, AcceptAction):
            return "in_bound_quote_must_accept"
        return None

    if task_input.remaining_turns <= 1:
        if not isinstance(action, RejectAction):
            return "final_out_of_bound_quote_must_reject"
        return None

    if not isinstance(action, ProposeAction):
        return "out_of_bound_quote_must_counter"
    if limit_price is not None and action.price != limit_price:
        return "counter_must_equal_limit"
    return None


__all__ = [
    "CandidateViolation",
    "DecideCandidateViolation",
    "NegotiationCandidateViolation",
    "decide_candidate_violation",
    "negotiation_candidate_violation",
]
