"""
Negotiation Protocol — A2A Multi-Turn with Arena Performance Tracking

Each negotiation session is a "battle" in the Arena.
The protocol produces deterministic outcomes that drive ELO updates.
"""

from __future__ import annotations

import uuid
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, TYPE_CHECKING

from .engine import Intent, CandidateMatch, PriceConstraint, NegotiationStyle, TradingRules

if TYPE_CHECKING:
    from .arena import Arena, BattleOutcome


# ============================================================
# States & Types
# ============================================================


class NegotiationState(str, Enum):
    IDLE = "idle"
    OFFER_SENT = "offer_sent"
    COUNTER_OFFER = "counter_offer"
    EVALUATING = "evaluating"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class ProposalType(str, Enum):
    INITIAL_OFFER = "initial_offer"
    COUNTER = "counter"
    FINAL_OFFER = "final_offer"


@dataclass
class Proposal:
    proposal_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    round: int = 0
    proposal_type: ProposalType = ProposalType.INITIAL_OFFER
    price: float = 0.0
    quantity: int = 1
    currency: str = "INJ"
    message: str = ""
    terms: dict = field(default_factory=dict)
    sender_intent_id: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class NegotiationSession:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    match: CandidateMatch = None  # type: ignore
    state: NegotiationState = NegotiationState.IDLE
    proposals: list[Proposal] = field(default_factory=list)
    current_round: int = 0
    started_at: float = field(default_factory=time.time)
    arena_battle_started: bool = False  # linked to Arena yet?

    @property
    def buyer(self) -> Intent:
        return self.match.buy_intent

    @property
    def seller(self) -> Intent:
        return self.match.sell_intent

    @property
    def max_rounds(self) -> int:
        return min(self.buyer.rules.max_negotiation_rounds,
                   self.seller.rules.max_negotiation_rounds)

    @property
    def is_expired(self) -> bool:
        timeout = min(self.buyer.rules.timeout_seconds,
                      self.seller.rules.timeout_seconds)
        return (time.time() - self.started_at) > timeout

    def last_proposal(self) -> Optional[Proposal]:
        return self.proposals[-1] if self.proposals else None

    def last_proposal_by(self, intent_id: str) -> Optional[Proposal]:
        for p in reversed(self.proposals):
            if p.sender_intent_id == intent_id:
                return p
        return None

    def determine_winner(self) -> Optional[str]:
        """Determine who 'won' the negotiation for Arena ELO purposes."""
        if self.state != NegotiationState.ACCEPTED or not self.proposals:
            return None
        final = self.last_proposal()
        if not final:
            return None

        buyer = self.buyer
        seller = self.seller

        # Buyer wins if final price <= buyer's ideal
        # Seller wins if final price >= seller's ideal
        buyer_win = final.price <= buyer.price.ideal
        seller_win = final.price >= seller.price.ideal

        if buyer_win and not seller_win:
            return buyer.agent_id
        elif seller_win and not buyer_win:
            return seller.agent_id
        elif buyer_win and seller_win:
            # Both win — pick the one closer to their ideal
            buyer_gap = abs(final.price - buyer.price.ideal) / buyer.price.ideal if buyer.price.ideal else 0
            seller_gap = abs(final.price - seller.price.ideal) / seller.price.ideal if seller.price.ideal else 0
            return buyer.agent_id if buyer_gap < seller_gap else seller.agent_id
        else:
            # No winner — price in middle zone
            return None


# ============================================================
# Rule-Based Validators
# ============================================================


class ProposalValidator:
    """Validates proposals against human-defined rules. Core calibration layer."""

    @staticmethod
    def validate_initial_offer(
        proposal: Proposal, sender: Intent, counterparty: Intent
    ) -> tuple[bool, str]:
        if sender.intent_type.value == "sell":
            if proposal.price < sender.price.min_acceptable:
                return False, (
                    f"Ask {proposal.price} below your minimum {sender.price.min_acceptable}"
                )
            if proposal.price < sender.price.ideal:
                return False, (
                    f"Ask {proposal.price} below your ideal {sender.price.ideal}. Open at or above ideal."
                )
        else:
            if proposal.price > sender.price.max_acceptable:
                return False, (
                    f"Bid {proposal.price} above your maximum {sender.price.max_acceptable}"
                )
            if proposal.price > sender.price.ideal:
                return False, (
                    f"Bid {proposal.price} above your ideal {sender.price.ideal}. Open at or below ideal."
                )
        if proposal.quantity > counterparty.quantity:
            return False, f"Quantity {proposal.quantity} exceeds counterparty's {counterparty.quantity}"
        return True, "valid"

    @staticmethod
    def validate_counter_offer(
        proposal: Proposal,
        sender: Intent,
        counterparty: Intent,
        previous_proposal: Proposal,
    ) -> tuple[bool, str]:
        if sender.intent_type.value == "sell":
            if proposal.price > previous_proposal.price:
                return False, "Seller counter must not increase price"
            if proposal.price < sender.price.min_acceptable:
                return False, f"Price {proposal.price} below floor {sender.price.min_acceptable}"
        else:
            if proposal.price < previous_proposal.price:
                return False, "Buyer counter must not decrease price"
            if proposal.price > sender.price.max_acceptable:
                return False, f"Price {proposal.price} above ceiling {sender.price.max_acceptable}"

        min_delta = sender.rules.min_price_delta_pct / 100.0 * previous_proposal.price
        actual_delta = abs(proposal.price - previous_proposal.price)
        if 0 < actual_delta < min_delta:
            return False, (
                f"Price delta {actual_delta:.2f} below minimum {min_delta:.2f}"
            )
        return True, "valid"

    @staticmethod
    def check_auto_accept(proposal: Proposal, evaluator: Intent) -> tuple[bool, str]:
        threshold = evaluator.rules.auto_accept_threshold_pct / 100.0
        ideal = evaluator.price.ideal
        if ideal <= 0:
            return False, "no ideal set"
        deviation = abs(proposal.price - ideal) / ideal
        if deviation <= threshold:
            return True, f"Within {threshold*100:.1f}% of ideal — auto-accept"
        return False, f"Deviation {deviation*100:.1f}% > threshold {threshold*100:.1f}%"


# ============================================================
# Negotiation Protocol
# ============================================================


class NegotiationProtocol:
    """
    Orchestrates A2A negotiation with Arena integration.

    Each session can be linked to an Arena battle for ELO tracking.
    """

    NEGOTIATION_EXTENSION_URI = "https://adx.agentic.payment/negotiation/v1"

    def __init__(self, arena: Optional["Arena"] = None):
        self._sessions: dict[str, NegotiationSession] = {}
        self._validator = ProposalValidator()
        self._arena = arena

    # ---- Session ----

    def create_session(self, match: CandidateMatch) -> NegotiationSession:
        session = NegotiationSession(match=match)
        self._sessions[session.session_id] = session
        return session

    def get_session(self, session_id: str) -> Optional[NegotiationSession]:
        return self._sessions.get(session_id)

    # ---- Arena Integration ----

    def link_arena(self, session: NegotiationSession):
        """Start an Arena battle for this negotiation session."""
        if not self._arena or session.arena_battle_started:
            return

        buyer_agent = self._arena.registry.get(session.buyer.agent_id)
        seller_agent = self._arena.registry.get(session.seller.agent_id)

        if buyer_agent and seller_agent:
            self._arena.start_battle(
                buyer_agent=buyer_agent,
                seller_agent=seller_agent,
                session_id=session.session_id,
                asset_class=session.buyer.asset_class.value,
                description=f"{session.buyer.description} ↔ {session.seller.description}",
                buyer_ideal=session.buyer.price.ideal,
                seller_ideal=session.seller.price.ideal,
                quantity=session.buyer.quantity,
                currency=session.buyer.price.currency,
            )
            session.arena_battle_started = True

    def end_battle(self, session: NegotiationSession):
        """Report negotiation result to Arena for ELO update."""
        if not self._arena or not session.arena_battle_started:
            return

        from .arena import BattleOutcome

        final_price = session.last_proposal().price if session.last_proposal() else 0
        winner_id = session.determine_winner() or ""

        if session.state == NegotiationState.ACCEPTED:
            if winner_id:
                if winner_id == session.buyer.agent_id:
                    outcome = BattleOutcome.BUYER_WIN
                else:
                    outcome = BattleOutcome.SELLER_WIN
            else:
                outcome = BattleOutcome.DRAW
        elif session.state == NegotiationState.REJECTED:
            # Last proposer's counterparty rejected → proposer "surrendered"?
            last = session.last_proposal()
            if last and last.sender_intent_id == session.buyer.intent_id:
                outcome = BattleOutcome.BUYER_SURRENDER
            else:
                outcome = BattleOutcome.SELLER_SURRENDER
        else:
            outcome = BattleOutcome.TIMEOUT

        self._arena.end_battle(
            session_id=session.session_id,
            outcome=outcome,
            final_price=final_price,
            rounds_taken=session.current_round,
            winner_agent_id=winner_id,
        )

    # ---- Proposal Processing ----

    def process_proposal(
        self, session: NegotiationSession, proposal: Proposal, sender: Intent
    ) -> dict:
        """Process proposal, validate, and advance state machine."""
        counterparty = session.seller if sender.intent_type.value == "buy" else session.buyer

        # Start Arena battle on first real proposal
        if not session.arena_battle_started:
            self.link_arena(session)

        # Validate
        own_previous = session.last_proposal_by(sender.intent_id)
        if not own_previous:
            # Sender's first proposal — validate as initial offer against their own constraints
            valid, reason = self._validator.validate_initial_offer(proposal, sender, counterparty)
        else:
            # Sender has proposed before — validate as counter-offer (must move toward counterparty)
            valid, reason = self._validator.validate_counter_offer(
                proposal, sender, counterparty, own_previous
            )

        if not valid:
            return self._build_response(session, "invalid_proposal", error=reason)

        # Record
        proposal.round = session.current_round + 1
        session.proposals.append(proposal)
        session.current_round = proposal.round

        # Auto-accept check for counterparty
        auto_accept, auto_reason = self._validator.check_auto_accept(proposal, counterparty)
        if auto_accept:
            session.state = NegotiationState.ACCEPTED
            self.end_battle(session)
            return self._build_response(
                session, "accepted",
                message=f"Auto-accepted: {auto_reason}",
                final_price=proposal.price,
            )

        # Terminal conditions
        if session.current_round >= session.max_rounds:
            zone = session.match.price_zone
            if zone[0] <= proposal.price <= zone[1]:
                session.state = NegotiationState.ACCEPTED
                self.end_battle(session)
                return self._build_response(
                    session, "accepted",
                    message="Final round — price within acceptable zone",
                    final_price=proposal.price,
                )
            else:
                session.state = NegotiationState.REJECTED
                self.end_battle(session)
                return self._build_response(
                    session, "rejected",
                    message=f"Max rounds ({session.max_rounds}) reached",
                )

        if session.is_expired:
            session.state = NegotiationState.TIMEOUT
            self.end_battle(session)
            return self._build_response(session, "timeout", message="Negotiation time expired")

        # Continue
        session.state = NegotiationState.COUNTER_OFFER
        return self._build_response(
            session, "counter_proposal",
            message=f"Round {session.current_round}/{session.max_rounds}: awaiting counter",
            last_price=proposal.price,
            price_zone=list(session.match.price_zone),
        )

    def accept(self, session: NegotiationSession, accepter: Intent) -> dict:
        if not session.proposals:
            return self._build_response(session, "error", error="No proposal to accept")
        session.state = NegotiationState.ACCEPTED
        self.end_battle(session)
        last = session.last_proposal()
        return self._build_response(
            session, "accepted",
            message=f"Accepted by {accepter.agent_id}",
            final_price=last.price if last else 0,
        )

    def reject(self, session: NegotiationSession, rejecter: Intent, reason: str = "") -> dict:
        session.state = NegotiationState.REJECTED
        self.end_battle(session)
        return self._build_response(
            session, "rejected",
            message=f"Rejected by {rejecter.agent_id}: {reason}",
        )

    # ---- A2A Serialization ----

    @staticmethod
    def to_a2a_metadata(proposal: Proposal) -> dict:
        return {
            f"{NegotiationProtocol.NEGOTIATION_EXTENSION_URI}": {
                "proposal_id": proposal.proposal_id,
                "round": proposal.round,
                "proposal_type": proposal.proposal_type.value,
                "price": proposal.price,
                "quantity": proposal.quantity,
                "currency": proposal.currency,
                "message": proposal.message,
                "terms": proposal.terms,
                "sender_intent_id": proposal.sender_intent_id,
                "timestamp": proposal.timestamp,
            }
        }

    @staticmethod
    def from_a2a_metadata(metadata: dict) -> Optional[Proposal]:
        ext = metadata.get(NegotiationProtocol.NEGOTIATION_EXTENSION_URI)
        if not ext:
            return None
        return Proposal(
            proposal_id=ext.get("proposal_id", uuid.uuid4().hex),
            round=ext.get("round", 0),
            proposal_type=ProposalType(ext.get("proposal_type", "initial_offer")),
            price=ext.get("price", 0.0),
            quantity=ext.get("quantity", 1),
            currency=ext.get("currency", "INJ"),
            message=ext.get("message", ""),
            terms=ext.get("terms", {}),
            sender_intent_id=ext.get("sender_intent_id", ""),
            timestamp=ext.get("timestamp", time.time()),
        )

    @staticmethod
    def a2a_extension_header() -> dict:
        return {
            "uri": NegotiationProtocol.NEGOTIATION_EXTENSION_URI,
            "description": "ADX Agent Arena — Bid/Ask Negotiation Protocol v2",
            "required": False,
        }

    def _build_response(self, session: NegotiationSession, action: str, **kw) -> dict:
        return {
            "session_id": session.session_id,
            "action": action,
            "state": session.state.value,
            "round": session.current_round,
            "max_rounds": session.max_rounds,
            "buyer_agent_id": session.buyer.agent_id,
            "seller_agent_id": session.seller.agent_id,
            "price_zone": list(session.match.price_zone),
            **kw,
        }
