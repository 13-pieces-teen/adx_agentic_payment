"""Shared deterministic validation for Runtime candidates and Arena apply.

The Hosted Runtime uses these checks only to spend its one bounded correction
attempt more usefully. Arena remains authoritative and repeats the same checks
when it projects a persisted result.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, TypeAlias

from arena_agent_contracts import (
    AcceptAction,
    ArenaDecideInputV1,
    ArenaMarketIntentInputV1,
    ArenaMarketRfqInputV1,
    ArenaMarketSelectInputV1,
    ArenaNegotiateInputV1,
    BuyAction,
    DecideActionV1,
    EngageRequestActionV1,
    MarketIntentActionV1,
    MarketRfqActionV1,
    MarketSelectionActionV1,
    NegotiateActionV1,
    PassAction,
    ProposeAction,
    RequestNegotiationsActionV1,
    SellAction,
)


DecideCandidateViolation: TypeAlias = Literal[
    "action_not_allowed",
    "good_not_allowed",
    "insufficient_inventory",
    "insufficient_cash",
    "price_precision_exceeded",
    "public_price_not_allowed",
]
MarketIntentCandidateViolation: TypeAlias = Literal[
    "action_not_allowed",
    "good_not_allowed",
    "insufficient_inventory",
    "insufficient_cash",
    "market_price_required",
    "market_price_boundary_violation",
    "price_precision_exceeded",
]
MarketRfqCandidateViolation: TypeAlias = Literal[
    "rfq_budget_exceeded",
    "rfq_target_not_visible",
    "limit_price_violation",
    "price_precision_exceeded",
]
MarketSelectCandidateViolation: TypeAlias = Literal[
    "request_not_visible",
    "insufficient_inventory",
]
NegotiationCandidateViolation: TypeAlias = Literal[
    "buyer_opening_proposal_required",
    "counterparty_proposal_required",
    "final_turn_must_close",
    "limit_price_violation",
    "price_precision_exceeded",
]
CandidateViolation: TypeAlias = (
    DecideCandidateViolation
    | NegotiationCandidateViolation
    | MarketIntentCandidateViolation
    | MarketRfqCandidateViolation
    | MarketSelectCandidateViolation
)


_ARENA_MONEY_SCALE = Decimal(1_000_000)


def _has_arena_money_precision(value: Decimal) -> bool:
    atomic = value * _ARENA_MONEY_SCALE
    return atomic == atomic.to_integral_value()


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
    if action.public_price is not None or action.message is not None:
        return "public_price_not_allowed"
    if (
        action.limit_price is not None
        and not _has_arena_money_precision(action.limit_price)
    ):
        return "price_precision_exceeded"
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


def market_intent_candidate_violation(
    task_input: ArenaMarketIntentInputV1,
    action: MarketIntentActionV1,
) -> MarketIntentCandidateViolation | None:
    """Validate a real Agent's public listing and private hard boundary."""

    limits = task_input.limits
    if action.action not in limits.allowed_actions:
        return "action_not_allowed"
    if isinstance(action, PassAction):
        return None
    if not isinstance(action, (BuyAction, SellAction)):
        raise TypeError("action must be a market intent action")
    if limits.allowed_goods and action.good not in limits.allowed_goods:
        return "good_not_allowed"
    if action.public_price is None or action.limit_price is None:
        return "market_price_required"
    if not _has_arena_money_precision(
        action.public_price
    ) or not _has_arena_money_precision(action.limit_price):
        return "price_precision_exceeded"
    if isinstance(action, BuyAction):
        if action.public_price > action.limit_price:
            return "market_price_boundary_violation"
        if action.limit_price * action.quantity > task_input.cash:
            return "insufficient_cash"
    else:
        if action.public_price < action.limit_price:
            return "market_price_boundary_violation"
        if task_input.holdings.get(action.good, 0) < action.quantity:
            return "insufficient_inventory"
    return None


def market_rfq_candidate_violation(
    task_input: ArenaMarketRfqInputV1,
    action: MarketRfqActionV1,
) -> MarketRfqCandidateViolation | None:
    """Validate targets only; Arena never ranks or selects one for the buyer."""

    if isinstance(action, PassAction):
        return None
    if not isinstance(action, RequestNegotiationsActionV1):
        raise TypeError("action must be a market RFQ action")
    if len(action.requests) > task_input.max_outbound_rfq:
        return "rfq_budget_exceeded"
    visible = {entry.intent_id for entry in task_input.directory}
    for request in action.requests:
        if request.target_intent_id not in visible:
            return "rfq_target_not_visible"
        if not _has_arena_money_precision(request.opening_price):
            return "price_precision_exceeded"
        if request.opening_price > task_input.limit_price:
            return "limit_price_violation"
    return None


def market_select_candidate_violation(
    task_input: ArenaMarketSelectInputV1,
    action: MarketSelectionActionV1,
) -> MarketSelectCandidateViolation | None:
    """Validate seller ownership without choosing which RFQ to engage."""

    if isinstance(action, EngageRequestActionV1):
        if action.request_id not in {
            request.request_id for request in task_input.requests
        }:
            return "request_not_visible"
        if task_input.inventory_available < task_input.quantity:
            return "insufficient_inventory"
    return None


def negotiation_candidate_violation(
    task_input: ArenaNegotiateInputV1,
    action: NegotiateActionV1,
) -> NegotiationCandidateViolation | None:
    """Validate protocol shape and private bounds without choosing strategy."""

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
    if isinstance(action, ProposeAction) and not _has_arena_money_precision(
        action.price
    ):
        return "price_precision_exceeded"

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

    return None


__all__ = [
    "CandidateViolation",
    "DecideCandidateViolation",
    "MarketIntentCandidateViolation",
    "MarketRfqCandidateViolation",
    "MarketSelectCandidateViolation",
    "NegotiationCandidateViolation",
    "decide_candidate_violation",
    "market_intent_candidate_violation",
    "market_rfq_candidate_violation",
    "market_select_candidate_violation",
    "negotiation_candidate_violation",
]
