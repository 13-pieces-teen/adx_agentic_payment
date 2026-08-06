"""Arena invariants for an Agent-driven, Gateway-mediated market.

This module is an executable protocol oracle, not a trading Agent. It accepts
already-authored Agent candidate actions and enforces identity, ownership,
price, idempotency, and single-engagement invariants. It deliberately contains
no counterparty ranking, selection, or negotiation strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from arena_agent_contracts import (
    ARENA_MARKET_PROTOCOL_VERSION_V1,
    MarketDirectoryEntryV1,
    MarketDirectoryV1,
)

from .goods import GoodId
from .money import format_gold


MAX_OUTBOUND_RFQ = 3
MAX_NEGOTIATION_ACTIONS = 3
NEGOTIATE_STAGE_ACTION_SLOTS = (
    MAX_OUTBOUND_RFQ * MAX_NEGOTIATION_ACTIONS
    + (MAX_OUTBOUND_RFQ - 1)
)
MARKET_AFTER_DECIDE_ACTION_SLOTS = 1 + NEGOTIATE_STAGE_ACTION_SLOTS

MarketSide = Literal["buy", "sell"]
RequestStatus = Literal[
    "pending",
    "engaged",
    "rejected",
    "counterparty_busy",
]
EngagementStatus = Literal[
    "active",
    "accepted_pending_settlement",
    "rejected",
    "timed_out",
]


class AgentMarketError(ValueError):
    """A safe protocol-invariant failure."""


def _require_identifier(value: str, field: str) -> None:
    if not value or len(value) > 512:
        raise AgentMarketError(f"{field} must be a non-empty bounded identifier")


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AgentMarketError(f"{field} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AgentMarketIntent:
    """One Agent-authored round intent with a private hard price boundary."""

    intent_id: str
    source_result_id: str
    game_id: str
    round_id: str
    participant_id: str
    agent_id: str
    display_name: str
    side: MarketSide
    good: GoodId
    public_price_atomic: int
    limit_price_atomic: int
    expires_at: datetime
    public_message: str | None = None
    failed_negotiations: int = 0
    quantity: int = 1

    def __post_init__(self) -> None:
        for field in (
            "intent_id",
            "source_result_id",
            "game_id",
            "round_id",
            "participant_id",
            "agent_id",
        ):
            _require_identifier(str(getattr(self, field)), field)
        if not self.display_name.strip() or len(self.display_name) > 100:
            raise AgentMarketError(
                "display_name must contain 1-100 visible characters"
            )
        if self.side not in {"buy", "sell"}:
            raise AgentMarketError("side must be buy or sell")
        if self.quantity != 1:
            raise AgentMarketError("Agent-driven market quantity must be exactly 1")
        if self.public_price_atomic <= 0 or self.limit_price_atomic <= 0:
            raise AgentMarketError("public and limit prices must be positive")
        if self.side == "buy" and self.public_price_atomic > self.limit_price_atomic:
            raise AgentMarketError("buyer public price cannot exceed its private ceiling")
        if self.side == "sell" and self.public_price_atomic < self.limit_price_atomic:
            raise AgentMarketError("seller public price cannot be below its private floor")
        if self.failed_negotiations < 0:
            raise AgentMarketError("failed_negotiations cannot be negative")
        _require_aware(self.expires_at, "expires_at")
        if self.public_message is not None:
            if not self.public_message.strip() or len(self.public_message) > 100:
                raise AgentMarketError(
                    "public_message must contain 1-100 visible characters"
                )


@dataclass(frozen=True, slots=True)
class AgentNegotiationRequest:
    """A buyer-authored request targeting one seller intent."""

    request_id: str
    source_result_id: str
    buyer_intent_id: str
    seller_intent_id: str
    opening_price_atomic: int
    public_message: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field in (
            "request_id",
            "source_result_id",
            "buyer_intent_id",
            "seller_intent_id",
        ):
            _require_identifier(str(getattr(self, field)), field)
        if self.opening_price_atomic <= 0:
            raise AgentMarketError("opening price must be positive")
        if not self.public_message.strip() or len(self.public_message) > 100:
            raise AgentMarketError(
                "public_message must contain 1-100 visible characters"
            )
        _require_aware(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class AgentMarketEngagement:
    """A seller-selected RFQ with both Participant round slots reserved."""

    engagement_id: str
    negotiation_id: str
    request_id: str
    buyer_intent_id: str
    seller_intent_id: str
    buyer_participant_id: str
    seller_participant_id: str
    good: GoodId
    selection_result_id: str
    status: EngagementStatus = "active"
    terminal_source_result_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentMarketDeal:
    """Immutable accepted terms with Agent-authored proposal and acceptance."""

    deal_id: str
    engagement_id: str
    request_id: str
    buyer_participant_id: str
    seller_participant_id: str
    good: GoodId
    quantity: int
    unit_price_atomic: int
    latest_proposal_result_id: str
    acceptance_result_id: str
    accepted_by_participant_id: str


@dataclass(slots=True)
class _RequestRecord:
    request: AgentNegotiationRequest
    status: RequestStatus = "pending"
    engagement_id: str | None = None


class AgentDrivenMarket:
    """Mutable protocol oracle fed exclusively by explicit Agent actions."""

    def __init__(self, *, max_outbound_rfq: int = MAX_OUTBOUND_RFQ) -> None:
        if max_outbound_rfq < 1 or max_outbound_rfq > MAX_OUTBOUND_RFQ:
            raise AgentMarketError(
                f"max_outbound_rfq must be between 1 and {MAX_OUTBOUND_RFQ}"
            )
        self._max_outbound_rfq = max_outbound_rfq
        self._result_claims: dict[str, tuple[str, str]] = {}
        self._intents: dict[str, AgentMarketIntent] = {}
        self._intent_by_participant_round: dict[tuple[str, str, str], str] = {}
        self._intent_by_result: dict[str, str] = {}
        self._requests: dict[str, _RequestRecord] = {}
        self._request_ids_by_result: dict[str, tuple[str, ...]] = {}
        self._request_targets_by_buyer: dict[str, set[str]] = {}
        self._engagements: dict[str, AgentMarketEngagement] = {}
        self._engagement_by_selection_result: dict[str, str] = {}
        self._reserved_slots: dict[tuple[str, str, str], str] = {}
        self._deals: dict[str, AgentMarketDeal] = {}

    def publish_intent(self, intent: AgentMarketIntent) -> AgentMarketIntent:
        existing_id = self._intent_by_result.get(intent.source_result_id)
        if existing_id is not None:
            existing = self._intents[existing_id]
            if existing != intent:
                raise AgentMarketError("source Result already published another intent")
            return existing

        existing = self._intents.get(intent.intent_id)
        if existing is not None:
            if existing != intent:
                raise AgentMarketError("intent ID already exists with another payload")
            return existing

        participant_key = (
            intent.game_id,
            intent.round_id,
            intent.participant_id,
        )
        if participant_key in self._intent_by_participant_round:
            raise AgentMarketError("Participant already published a round intent")

        self._claim_results(
            (intent.source_result_id, "intent", intent.intent_id),
        )
        self._intents[intent.intent_id] = intent
        self._intent_by_result[intent.source_result_id] = intent.intent_id
        self._intent_by_participant_round[participant_key] = intent.intent_id
        return intent

    def public_directory(
        self,
        *,
        requester_intent_id: str,
        observed_at: datetime,
    ) -> MarketDirectoryV1:
        """Return an unranked, stable snapshot of structurally eligible intents."""

        _require_aware(observed_at, "observed_at")
        requester = self._require_intent(requester_intent_id)
        if requester.side != "buy":
            raise AgentMarketError("only a buyer intent can discover sell intents in v1")
        if requester.expires_at <= observed_at:
            raise AgentMarketError("requester intent is expired")

        entries: list[MarketDirectoryEntryV1] = []
        for candidate in self._intents.values():
            if candidate.side != "sell":
                continue
            if candidate.game_id != requester.game_id:
                continue
            if candidate.round_id != requester.round_id:
                continue
            if candidate.participant_id == requester.participant_id:
                continue
            if candidate.good != requester.good:
                continue
            if candidate.expires_at <= observed_at:
                continue
            if requester.limit_price_atomic < candidate.limit_price_atomic:
                continue
            if self._is_reserved(candidate):
                continue
            entries.append(
                MarketDirectoryEntryV1(
                    intent_id=candidate.intent_id,
                    agent_id=candidate.agent_id,
                    display_name=candidate.display_name,
                    side="sell",
                    good=candidate.good,
                    quantity=1,
                    public_price=format_gold(candidate.public_price_atomic),
                    failed_negotiations=candidate.failed_negotiations,
                    expires_at=candidate.expires_at,
                )
            )

        entries.sort(key=lambda entry: entry.intent_id)
        expiry = min(
            [requester.expires_at, *(entry.expires_at for entry in entries)]
        )
        return MarketDirectoryV1(
            schema_version=ARENA_MARKET_PROTOCOL_VERSION_V1,
            market_session_id=f"market:{requester.game_id}:{requester.round_id}",
            game_id=requester.game_id,
            round_id=requester.round_id,
            entries=tuple(entries),
            expires_at=expiry,
        )

    def submit_request(
        self,
        request: AgentNegotiationRequest,
    ) -> AgentNegotiationRequest:
        return self.submit_requests((request,))[0]

    def submit_requests(
        self,
        requests: tuple[AgentNegotiationRequest, ...],
    ) -> tuple[AgentNegotiationRequest, ...]:
        """Apply one durable RFQ attempt from one Agent Result."""

        if len(requests) != 1:
            raise AgentMarketError(
                "one RFQ Task must target exactly one seller"
            )
        result_ids = {request.source_result_id for request in requests}
        buyer_intent_ids = {request.buyer_intent_id for request in requests}
        request_ids = [request.request_id for request in requests]
        seller_intent_ids = [request.seller_intent_id for request in requests]
        if len(result_ids) != 1:
            raise AgentMarketError("one RFQ batch requires one source Result")
        if len(buyer_intent_ids) != 1:
            raise AgentMarketError("one RFQ batch requires one buyer intent")
        if len(request_ids) != len(set(request_ids)):
            raise AgentMarketError("RFQ batch request IDs must be unique")
        if len(seller_intent_ids) != len(set(seller_intent_ids)):
            raise AgentMarketError("RFQ batch seller targets must be unique")

        source_result_id = requests[0].source_result_id
        existing_ids = self._request_ids_by_result.get(source_result_id)
        if existing_ids is not None:
            existing = tuple(
                self._requests[request_id].request
                for request_id in existing_ids
            )
            if existing != requests:
                raise AgentMarketError(
                    "source Result already submitted another RFQ batch"
                )
            return existing

        buyer = self._require_intent(requests[0].buyer_intent_id)
        if any(
            record.request.buyer_intent_id == buyer.intent_id
            and record.status in {"pending", "engaged"}
            for record in self._requests.values()
        ):
            raise AgentMarketError("buyer has an unresolved RFQ")
        targets = set(
            self._request_targets_by_buyer.get(
                buyer.intent_id,
                set(),
            )
        )
        if len(targets) + len(requests) > self._max_outbound_rfq:
            raise AgentMarketError("buyer exhausted the outbound RFQ budget")

        for request in requests:
            existing_record = self._requests.get(request.request_id)
            if existing_record is not None:
                if existing_record.request != request:
                    raise AgentMarketError(
                        "request ID already exists with another payload"
                    )
                raise AgentMarketError(
                    "request ID belongs to another source Result"
                )
            seller = self._require_intent(request.seller_intent_id)
            self._validate_counterparties(buyer=buyer, seller=seller)
            if (
                request.created_at >= buyer.expires_at
                or request.created_at >= seller.expires_at
            ):
                raise AgentMarketError("RFQ targets an expired intent")
            if request.opening_price_atomic > buyer.limit_price_atomic:
                raise AgentMarketError(
                    "opening price exceeds the buyer private ceiling"
                )
            if self._is_reserved(buyer) or self._is_reserved(seller):
                raise AgentMarketError("counterparty_busy")
            if seller.intent_id in targets:
                raise AgentMarketError(
                    "buyer already requested this seller intent"
                )

        self._claim_results(
            (
                source_result_id,
                "rfq",
                f"rfq-batch:{source_result_id}",
            ),
        )
        for request in requests:
            self._requests[request.request_id] = _RequestRecord(
                request=request
            )
            targets.add(request.seller_intent_id)
        self._request_targets_by_buyer[buyer.intent_id] = targets
        self._request_ids_by_result[source_result_id] = tuple(request_ids)
        return requests

    def inbound_requests(
        self,
        *,
        seller_participant_id: str,
    ) -> tuple[AgentNegotiationRequest, ...]:
        """Return a stable inbox; Arena does not rank requests for the seller."""

        requests = [
            record.request
            for record in self._requests.values()
            if record.status == "pending"
            and self._require_intent(
                record.request.seller_intent_id
            ).participant_id
            == seller_participant_id
        ]
        return tuple(sorted(requests, key=lambda value: value.request_id))

    def engage_request(
        self,
        *,
        actor_participant_id: str,
        request_id: str,
        selection_result_id: str,
    ) -> AgentMarketEngagement:
        _require_identifier(selection_result_id, "selection_result_id")
        selected_engagement_id = self._engagement_by_selection_result.get(
            selection_result_id
        )
        if selected_engagement_id is not None:
            engagement = self._engagements[selected_engagement_id]
            if (
                engagement.request_id != request_id
                or engagement.seller_participant_id != actor_participant_id
            ):
                raise AgentMarketError(
                    "selection Result already engaged another request"
                )
            return engagement

        record = self._require_request_record(request_id)
        if record.status != "pending":
            raise AgentMarketError("request is no longer pending")
        buyer = self._require_intent(record.request.buyer_intent_id)
        seller = self._require_intent(record.request.seller_intent_id)
        if seller.participant_id != actor_participant_id:
            raise AgentMarketError("only the target seller Agent can engage an RFQ")

        buyer_slot = self._slot_key(buyer)
        seller_slot = self._slot_key(seller)
        if buyer_slot in self._reserved_slots or seller_slot in self._reserved_slots:
            record.status = "counterparty_busy"
            raise AgentMarketError("counterparty_busy")

        engagement_id = f"engagement:{request_id}"
        engagement = AgentMarketEngagement(
            engagement_id=engagement_id,
            negotiation_id=f"negotiation:{request_id}",
            request_id=request_id,
            buyer_intent_id=buyer.intent_id,
            seller_intent_id=seller.intent_id,
            buyer_participant_id=buyer.participant_id,
            seller_participant_id=seller.participant_id,
            good=buyer.good,
            selection_result_id=selection_result_id,
        )
        self._claim_results(
            (selection_result_id, "engage", engagement_id),
        )
        self._engagements[engagement_id] = engagement
        self._engagement_by_selection_result[selection_result_id] = engagement_id
        self._reserved_slots[buyer_slot] = engagement_id
        self._reserved_slots[seller_slot] = engagement_id
        record.status = "engaged"
        record.engagement_id = engagement_id
        return engagement

    def close_engagement(
        self,
        *,
        engagement_id: str,
        status: Literal["rejected", "timed_out"],
        source_result_id: str | None,
    ) -> AgentMarketEngagement:
        engagement = self._require_engagement(engagement_id)
        if engagement.status != "active":
            if (
                engagement.status == status
                and engagement.terminal_source_result_id == source_result_id
            ):
                return engagement
            raise AgentMarketError("engagement is already terminal")
        if status == "rejected" and source_result_id is None:
            raise AgentMarketError("Agent rejection requires a source Result")
        if status == "timed_out" and source_result_id is not None:
            raise AgentMarketError("timeout must come from the Arena Finalizer")
        if source_result_id is not None:
            _require_identifier(source_result_id, "source_result_id")
            self._claim_results(
                (source_result_id, "reject", engagement_id),
            )

        closed = replace(
            engagement,
            status=status,
            terminal_source_result_id=source_result_id,
        )
        self._engagements[engagement_id] = closed
        self._requests[engagement.request_id].status = "rejected"
        self._release_slots(engagement)
        return closed

    def freeze_deal(
        self,
        *,
        engagement_id: str,
        latest_proposal_result_id: str,
        latest_proposal_actor_id: str,
        acceptance_result_id: str,
        accepted_by_participant_id: str,
        unit_price_atomic: int,
    ) -> AgentMarketDeal:
        """Freeze terms only from two different, explicit Agent results."""

        for field, value in (
            ("latest_proposal_result_id", latest_proposal_result_id),
            ("latest_proposal_actor_id", latest_proposal_actor_id),
            ("acceptance_result_id", acceptance_result_id),
            ("accepted_by_participant_id", accepted_by_participant_id),
        ):
            _require_identifier(value, field)
        if latest_proposal_result_id == acceptance_result_id:
            raise AgentMarketError("proposal and acceptance require distinct Results")

        engagement = self._require_engagement(engagement_id)
        deal_id = f"deal:{engagement_id}"
        existing = self._deals.get(deal_id)
        if existing is not None:
            if (
                existing.latest_proposal_result_id != latest_proposal_result_id
                or existing.acceptance_result_id != acceptance_result_id
                or existing.unit_price_atomic != unit_price_atomic
                or existing.accepted_by_participant_id
                != accepted_by_participant_id
            ):
                raise AgentMarketError("Deal is immutable")
            return existing
        if engagement.status != "active":
            raise AgentMarketError("only an active engagement can freeze a Deal")

        participants = {
            engagement.buyer_participant_id,
            engagement.seller_participant_id,
        }
        if {
            latest_proposal_actor_id,
            accepted_by_participant_id,
        } != participants:
            raise AgentMarketError(
                "proposal and acceptance must come from opposite counterparties"
            )

        buyer = self._require_intent(engagement.buyer_intent_id)
        seller = self._require_intent(engagement.seller_intent_id)
        request = self._require_request_record(engagement.request_id).request
        binding_opening = (
            latest_proposal_result_id == request.source_result_id
        )
        if binding_opening and (
            latest_proposal_actor_id != buyer.participant_id
            or unit_price_atomic != request.opening_price_atomic
        ):
            raise AgentMarketError(
                "binding RFQ proposal must preserve its buyer and opening price"
            )
        if (
            unit_price_atomic <= 0
            or unit_price_atomic > buyer.limit_price_atomic
            or unit_price_atomic < seller.limit_price_atomic
        ):
            raise AgentMarketError("accepted price violates a private hard limit")

        claims = [
            (
                acceptance_result_id,
                "acceptance",
                engagement_id,
            )
        ]
        if not binding_opening:
            claims.insert(
                0,
                (
                    latest_proposal_result_id,
                    "proposal",
                    engagement_id,
                ),
            )
        self._claim_results(*claims)
        deal = AgentMarketDeal(
            deal_id=deal_id,
            engagement_id=engagement_id,
            request_id=engagement.request_id,
            buyer_participant_id=engagement.buyer_participant_id,
            seller_participant_id=engagement.seller_participant_id,
            good=engagement.good,
            quantity=1,
            unit_price_atomic=unit_price_atomic,
            latest_proposal_result_id=latest_proposal_result_id,
            acceptance_result_id=acceptance_result_id,
            accepted_by_participant_id=accepted_by_participant_id,
        )
        self._deals[deal_id] = deal
        self._engagements[engagement_id] = replace(
            engagement,
            status="accepted_pending_settlement",
            terminal_source_result_id=acceptance_result_id,
        )
        return deal

    def request_status(self, request_id: str) -> RequestStatus:
        return self._require_request_record(request_id).status

    def slot_engagement(
        self,
        *,
        game_id: str,
        round_id: str,
        participant_id: str,
    ) -> str | None:
        return self._reserved_slots.get((game_id, round_id, participant_id))

    def _require_intent(self, intent_id: str) -> AgentMarketIntent:
        try:
            return self._intents[intent_id]
        except KeyError:
            raise AgentMarketError("market intent not found") from None

    def _require_request_record(self, request_id: str) -> _RequestRecord:
        try:
            return self._requests[request_id]
        except KeyError:
            raise AgentMarketError("negotiation request not found") from None

    def _require_engagement(self, engagement_id: str) -> AgentMarketEngagement:
        try:
            return self._engagements[engagement_id]
        except KeyError:
            raise AgentMarketError("engagement not found") from None

    @staticmethod
    def _slot_key(intent: AgentMarketIntent) -> tuple[str, str, str]:
        return intent.game_id, intent.round_id, intent.participant_id

    def _is_reserved(self, intent: AgentMarketIntent) -> bool:
        return self._slot_key(intent) in self._reserved_slots

    @staticmethod
    def _validate_counterparties(
        *,
        buyer: AgentMarketIntent,
        seller: AgentMarketIntent,
    ) -> None:
        if buyer.side != "buy" or seller.side != "sell":
            raise AgentMarketError("RFQ must target buy and sell intents")
        if buyer.participant_id == seller.participant_id:
            raise AgentMarketError("a Participant cannot negotiate with itself")
        if buyer.game_id != seller.game_id or buyer.round_id != seller.round_id:
            raise AgentMarketError("counterparty intents must share one Game round")
        if buyer.good != seller.good or buyer.quantity != seller.quantity:
            raise AgentMarketError("counterparty intents must share good and quantity")
        if buyer.limit_price_atomic < seller.limit_price_atomic:
            raise AgentMarketError("counterparty private limits do not overlap")

    def _release_slots(self, engagement: AgentMarketEngagement) -> None:
        buyer = self._require_intent(engagement.buyer_intent_id)
        seller = self._require_intent(engagement.seller_intent_id)
        for slot in (self._slot_key(buyer), self._slot_key(seller)):
            if self._reserved_slots.get(slot) == engagement.engagement_id:
                del self._reserved_slots[slot]

    def _claim_results(
        self,
        *claims: tuple[str, str, str],
    ) -> None:
        """Atomically prevent one AgentTaskResult from authoring two actions."""

        pending: dict[str, tuple[str, str]] = {}
        for result_id, action_kind, action_id in claims:
            claim = (action_kind, action_id)
            previous_pending = pending.get(result_id)
            if previous_pending is not None and previous_pending != claim:
                raise AgentMarketError(
                    "one source Result cannot author multiple market actions"
                )
            existing = self._result_claims.get(result_id)
            if existing is not None and existing != claim:
                raise AgentMarketError(
                    "source Result already authored another market action"
                )
            pending[result_id] = claim
        self._result_claims.update(pending)


__all__ = [
    "AgentDrivenMarket",
    "AgentMarketDeal",
    "AgentMarketEngagement",
    "AgentMarketError",
    "AgentMarketIntent",
    "AgentNegotiationRequest",
    "EngagementStatus",
    "MAX_OUTBOUND_RFQ",
    "MARKET_AFTER_DECIDE_ACTION_SLOTS",
    "MAX_NEGOTIATION_ACTIONS",
    "NEGOTIATE_STAGE_ACTION_SLOTS",
    "MarketSide",
    "RequestStatus",
]
