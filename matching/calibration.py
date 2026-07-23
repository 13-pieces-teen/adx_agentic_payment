"""
Calibration Strategies — Agent Negotiation Precision Without Fine-Tuning

With BYOAgent, calibration becomes even more critical:
the platform doesn't control the LLM, so we provide:
- Standardized prompt templates users can customize
- Rule validators that catch bad proposals from any LLM
- Few-shot examples users can learn from
- Performance feedback via Arena rankings
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .engine import Intent, NegotiationStyle, CandidateMatch
from .negotiation import NegotiationSession, Proposal


# ============================================================
# Structured Schemas (LLM function-calling)
# ============================================================

PROPOSAL_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "price": {"type": "number", "description": "Proposed price"},
        "quantity": {"type": "integer", "minimum": 1},
        "message": {"type": "string", "maxLength": 500,
                    "description": "Rationale (shown to counterparty agent)"},
        "proposal_type": {
            "type": "string",
            "enum": ["initial_offer", "counter", "final_offer"],
        },
        "terms": {
            "type": "object",
            "properties": {
                "delivery_timeline": {"type": "string"},
                "quality_guarantee": {"type": "string"},
            },
            "additionalProperties": True,
        },
    },
    "required": ["price", "quantity", "message", "proposal_type"],
}

EVALUATION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["accept", "counter", "reject"]},
        "counter_price": {"type": "number", "description": "Required if decision=counter"},
        "reasoning": {"type": "string", "maxLength": 500},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["decision", "reasoning", "confidence"],
}


# ============================================================
# Negotiation Profiles
# ============================================================


@dataclass
class NegotiationProfile:
    style: NegotiationStyle
    opening_margin_pct: float = 10.0
    concession_rate_pct: float = 20.0
    min_deal_score: float = 30.0
    patience_rounds: int = 3

    def to_prompt_context(self) -> str:
        return f"""You are a {self.style.value} negotiator.
- Open {self.opening_margin_pct}% away from ideal
- Concede ~{self.concession_rate_pct}% of gap per round
- After {self.patience_rounds} rounds without agreement, make final offer
- Only accept deals scoring above {self.min_deal_score}/100"""


PROFILES = {
    NegotiationStyle.AGGRESSIVE: NegotiationProfile(
        style=NegotiationStyle.AGGRESSIVE,
        opening_margin_pct=20.0, concession_rate_pct=10.0,
        min_deal_score=50.0, patience_rounds=5,
    ),
    NegotiationStyle.BALANCED: NegotiationProfile(
        style=NegotiationStyle.BALANCED,
        opening_margin_pct=10.0, concession_rate_pct=25.0,
        min_deal_score=30.0, patience_rounds=3,
    ),
    NegotiationStyle.PASSIVE: NegotiationProfile(
        style=NegotiationStyle.PASSIVE,
        opening_margin_pct=5.0, concession_rate_pct=40.0,
        min_deal_score=10.0, patience_rounds=2,
    ),
}


# ============================================================
# Outcome Scoring & Feedback Loop
# ============================================================


@dataclass
class NegotiationOutcome:
    session_id: str
    success: bool
    final_price: float
    buyer_ideal: float
    seller_ideal: float
    rounds_taken: int
    duration_seconds: float
    buyer_style: NegotiationStyle
    seller_style: NegotiationStyle
    asset_class: str
    timestamp: float = field(default_factory=time.time)

    def score(self) -> dict:
        midpoint = (self.buyer_ideal + self.seller_ideal) / 2
        deviation = abs(self.final_price - midpoint) / midpoint if midpoint > 0 else 0
        return {
            "price_efficiency": max(0, 100 - deviation * 100),
            "speed": max(0, 100 - (self.rounds_taken - 1) * 20),
            "success": 100 if self.success else 0,
            "composite": (
                max(0, 100 - deviation * 100) * 0.5
                + max(0, 100 - (self.rounds_taken - 1) * 20) * 0.3
                + (100 if self.success else 0) * 0.2
            ),
        }


class OutcomeLogger:
    def __init__(self):
        self._outcomes: list[NegotiationOutcome] = []

    def log(self, outcome: NegotiationOutcome):
        self._outcomes.append(outcome)

    def analyze(self, min_samples: int = 10) -> dict:
        if len(self._outcomes) < min_samples:
            return {"status": "insufficient_data", "samples": len(self._outcomes)}

        successes = [o for o in self._outcomes if o.success]
        failures = [o for o in self._outcomes if not o.success]

        failure_patterns = {}
        for o in failures:
            key = f"{o.buyer_style.value}_x_{o.seller_style.value}"
            failure_patterns[key] = failure_patterns.get(key, 0) + 1

        return {
            "total_outcomes": len(self._outcomes),
            "success_rate_pct": round(len(successes) / len(self._outcomes) * 100, 1),
            "avg_rounds_success": round(sum(o.rounds_taken for o in successes) / len(successes), 1) if successes else 0,
            "avg_rounds_failure": round(sum(o.rounds_taken for o in failures) / len(failures), 1) if failures else 0,
            "failure_by_style_pair": failure_patterns,
            "arena_ready": True,  # Arena provides richer feedback
        }


class ReviewEscalator:
    def __init__(
        self,
        low_confidence_threshold: float = 0.6,
        high_value_threshold: float = 1000.0,
        style_mismatch_threshold: int = 3,
    ):
        self.low_confidence_threshold = low_confidence_threshold
        self.high_value_threshold = high_value_threshold
        self.style_mismatch_threshold = style_mismatch_threshold

    def should_escalate(self, session: NegotiationSession, llm_confidence: float = 1.0) -> tuple[bool, str]:
        reasons = []
        last = session.last_proposal()
        if last and last.price * last.quantity >= self.high_value_threshold:
            reasons.append(f"High-value: {last.price * last.quantity}")
        if (session.buyer.negotiation_style == NegotiationStyle.AGGRESSIVE
                and session.seller.negotiation_style == NegotiationStyle.AGGRESSIVE):
            reasons.append("Aggressive vs Aggressive — deadlock risk")
        if llm_confidence < self.low_confidence_threshold:
            reasons.append(f"Low confidence: {llm_confidence:.2f}")
        if reasons:
            return True, "; ".join(reasons)
        return False, ""


# ============================================================
# BYOAgent Strategy Builder
# ============================================================


def build_agent_context(intent: Intent, match: CandidateMatch) -> dict:
    """Build context dict for an LLM call (BYOAgent pattern)."""
    profile = PROFILES.get(intent.negotiation_style, PROFILES[NegotiationStyle.BALANCED])
    counterparty = match.sell_intent if intent.intent_type.value == "buy" else match.buy_intent

    return {
        "role": intent.intent_type.value,
        "agent_id": intent.agent_id,
        "agent_name": intent.agent_name,
        "profile": {
            "style": profile.style.value,
            "opening_margin_pct": profile.opening_margin_pct,
            "concession_rate_pct": profile.concession_rate_pct,
            "patience_rounds": profile.patience_rounds,
        },
        "constraints": {
            "min_price": intent.price.min_acceptable,
            "ideal_price": intent.price.ideal,
            "max_price": intent.price.max_acceptable,
            "currency": intent.price.currency,
            "quantity": intent.quantity,
        },
        "rules": {
            "max_rounds": intent.rules.max_negotiation_rounds,
            "min_price_delta_pct": intent.rules.min_price_delta_pct,
            "auto_accept_threshold_pct": intent.rules.auto_accept_threshold_pct,
        },
        "asset": {
            "class": intent.asset_class.value,
            "description": intent.description,
            "tags": intent.tags,
        },
        "market_context": {
            "price_zone": list(match.price_zone),
            "counterparty_style": counterparty.negotiation_style.value,
            "counterparty_name": counterparty.agent_name,
            "counterparty_description": counterparty.description,
        },
    }
