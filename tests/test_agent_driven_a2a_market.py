"""Contracts and invariants for Agent-selected A2A market engagements."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from arena_agent_contracts import (
    ARENA_MARKET_PROTOCOL_VERSION_V1,
    MarketIntentActionV1,
    MarketSelectionActionV1,
    RequestNegotiationsActionV1,
)
from arena_game import (
    AgentDrivenMarket,
    AgentMarketError,
    AgentMarketIntent,
    AgentNegotiationRequest,
    gold,
)


NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _intent(
    intent_id: str,
    participant_id: str,
    side: str,
    *,
    good: str = "grain",
    public_price: str = "1.8",
    limit_price: str = "2.0",
    expires_at: datetime | None = None,
) -> AgentMarketIntent:
    return AgentMarketIntent(
        intent_id=intent_id,
        source_result_id=f"result:{intent_id}",
        game_id="game_1",
        round_id="round_1",
        participant_id=participant_id,
        agent_id=f"agent:{participant_id}",
        display_name=participant_id,
        side=side,  # type: ignore[arg-type]
        good=good,  # type: ignore[arg-type]
        public_price_atomic=gold(public_price),
        limit_price_atomic=gold(limit_price),
        expires_at=expires_at or NOW + timedelta(seconds=30),
        public_message=f"{side} {good}",
    )


def _request(
    request_id: str,
    buyer_intent_id: str,
    seller_intent_id: str,
    *,
    opening_price: str = "1.7",
) -> AgentNegotiationRequest:
    return AgentNegotiationRequest(
        request_id=request_id,
        source_result_id=f"result:{request_id}",
        buyer_intent_id=buyer_intent_id,
        seller_intent_id=seller_intent_id,
        opening_price_atomic=gold(opening_price),
        public_message="我选择与你协商。",
        created_at=NOW,
    )


def test_market_wire_actions_are_strict_versioned_agent_choices() -> None:
    action = TypeAdapter(MarketIntentActionV1).validate_python(
        {
            "action": "buy",
            "good": "grain",
            "publicPrice": "1.800000",
            "limitPrice": "2.000000",
            "message": "愿意协商。",
        }
    )
    assert action.public_price == Decimal("1.800000")
    assert action.limit_price == Decimal("2.000000")
    assert action.model_dump()["publicPrice"] == "1.800000"

    engage = TypeAdapter(MarketSelectionActionV1).validate_python(
        {"action": "engage", "requestId": "request_1"}
    )
    assert engage.request_id == "request_1"

    with pytest.raises(ValidationError):
        TypeAdapter(MarketIntentActionV1).validate_python(
            {
                "action": "sell",
                "good": "grain",
                "publicPrice": 1.8,
                "limitPrice": "1.600000",
            }
        )


def test_rfq_action_is_sequential_and_requires_exactly_one_target() -> None:
    action = RequestNegotiationsActionV1.model_validate(
        {
            "action": "request_negotiations",
            "requests": [
                {
                    "targetIntentId": "sell_1",
                    "openingPrice": "1.700000",
                    "message": "报价一",
                }
            ],
        }
    )
    assert len(action.requests) == 1

    for requests in (
        [],
        [
            {
                "targetIntentId": f"sell_{index + 1}",
                "openingPrice": "1.700000",
                "message": f"报价 {index + 1}",
            }
            for index in range(2)
        ],
    ):
        with pytest.raises(ValidationError):
            RequestNegotiationsActionV1.model_validate(
                {
                    "action": "request_negotiations",
                    "requests": requests,
                }
            )


def test_public_directory_filters_but_does_not_rank_or_expose_private_limits() -> None:
    market = AgentDrivenMarket()
    buyer = _intent("buyer", "buyer", "buy")
    eligible_b = _intent(
        "seller_b",
        "seller_b",
        "sell",
        public_price="1.9",
        limit_price="1.6",
    )
    eligible_a = _intent(
        "seller_a",
        "seller_a",
        "sell",
        public_price="1.7",
        limit_price="1.5",
    )
    incompatible_limit = _intent(
        "seller_expensive",
        "seller_expensive",
        "sell",
        public_price="2.2",
        limit_price="2.1",
    )
    wrong_good = _intent(
        "seller_iron",
        "seller_iron",
        "sell",
        good="iron",
        public_price="5",
        limit_price="4",
    )
    for intent in (
        buyer,
        eligible_b,
        eligible_a,
        incompatible_limit,
        wrong_good,
    ):
        market.publish_intent(intent)

    directory = market.public_directory(
        requester_intent_id=buyer.intent_id,
        observed_at=NOW,
    )
    assert directory.schema_version == ARENA_MARKET_PROTOCOL_VERSION_V1
    assert [entry.intent_id for entry in directory.entries] == [
        "seller_a",
        "seller_b",
    ]
    wire = directory.model_dump()
    assert "limitPrice" not in str(wire)
    assert wire["entries"][0]["publicPrice"] == "1.7"


def test_buyer_authors_rfq_and_seller_agent_authors_engagement() -> None:
    market = AgentDrivenMarket()
    buyer = market.publish_intent(_intent("buyer", "buyer", "buy"))
    seller = market.publish_intent(
        _intent(
            "seller",
            "seller",
            "sell",
            public_price="1.9",
            limit_price="1.6",
        )
    )
    request = market.submit_request(_request("request_1", buyer.intent_id, seller.intent_id))

    assert market.inbound_requests(seller_participant_id="seller") == (request,)
    with pytest.raises(
        AgentMarketError,
        match="only the target seller Agent",
    ):
        market.engage_request(
            actor_participant_id="buyer",
            request_id=request.request_id,
            selection_result_id="result:illegal-selection",
        )

    engagement = market.engage_request(
        actor_participant_id="seller",
        request_id=request.request_id,
        selection_result_id="result:seller-engage",
    )
    assert engagement.request_id == request.request_id
    assert engagement.selection_result_id == "result:seller-engage"
    assert market.request_status(request.request_id) == "engaged"
    assert (
        market.slot_engagement(
            game_id="game_1",
            round_id="round_1",
            participant_id="buyer",
        )
        == engagement.engagement_id
    )
    assert (
        market.engage_request(
            actor_participant_id="seller",
            request_id=request.request_id,
            selection_result_id="result:seller-engage",
        )
        == engagement
    )


def test_buyer_uses_three_durable_sequential_rfq_attempts() -> None:
    market = AgentDrivenMarket()
    buyer = market.publish_intent(_intent("buyer", "buyer", "buy"))
    sellers = tuple(
        market.publish_intent(
            _intent(
                f"seller_{index}",
                f"seller_{index}",
                "sell",
                public_price="1.9",
                limit_price="1.6",
            )
        )
        for index in range(4)
    )
    requests = tuple(
        _request(
            f"request_{index}",
            buyer.intent_id,
            seller.intent_id,
        )
        for index, seller in enumerate(sellers)
    )

    assert market.submit_request(requests[0]) == requests[0]
    with pytest.raises(AgentMarketError, match="unresolved RFQ"):
        market.submit_request(requests[1])

    for index, request in enumerate(requests[:3]):
        if index > 0:
            assert market.submit_request(request) == request
        engagement = market.engage_request(
            actor_participant_id=f"seller_{index}",
            request_id=request.request_id,
            selection_result_id=f"result:select-{index}",
        )
        market.close_engagement(
            engagement_id=engagement.engagement_id,
            status="rejected",
            source_result_id=f"result:reject-{index}",
        )

    with pytest.raises(AgentMarketError, match="exhausted"):
        market.submit_request(requests[3])


def test_one_seller_cannot_engage_two_buyers_in_the_same_round() -> None:
    market = AgentDrivenMarket()
    buyer_a = market.publish_intent(_intent("buyer_a", "buyer_a", "buy"))
    buyer_b = market.publish_intent(_intent("buyer_b", "buyer_b", "buy"))
    seller = market.publish_intent(
        _intent(
            "seller",
            "seller",
            "sell",
            public_price="1.9",
            limit_price="1.6",
        )
    )
    request_a = market.submit_request(
        _request("request_a", buyer_a.intent_id, seller.intent_id)
    )
    request_b = market.submit_request(
        _request("request_b", buyer_b.intent_id, seller.intent_id)
    )

    market.engage_request(
        actor_participant_id="seller",
        request_id=request_a.request_id,
        selection_result_id="result:select-a",
    )
    with pytest.raises(AgentMarketError, match="counterparty_busy"):
        market.engage_request(
            actor_participant_id="seller",
            request_id=request_b.request_id,
            selection_result_id="result:select-b",
        )
    assert market.request_status(request_b.request_id) == "counterparty_busy"


def test_rejection_requires_an_agent_result_and_releases_fallback_slots() -> None:
    market = AgentDrivenMarket()
    buyer = market.publish_intent(_intent("buyer", "buyer", "buy"))
    seller_a = market.publish_intent(
        _intent(
            "seller_a",
            "seller_a",
            "sell",
            public_price="1.9",
            limit_price="1.6",
        )
    )
    seller_b = market.publish_intent(
        _intent(
            "seller_b",
            "seller_b",
            "sell",
            public_price="1.8",
            limit_price="1.5",
        )
    )
    request_a = market.submit_request(
        _request("request_a", buyer.intent_id, seller_a.intent_id)
    )
    engagement_a = market.engage_request(
        actor_participant_id="seller_a",
        request_id=request_a.request_id,
        selection_result_id="result:select-a",
    )

    with pytest.raises(AgentMarketError, match="requires a source Result"):
        market.close_engagement(
            engagement_id=engagement_a.engagement_id,
            status="rejected",
            source_result_id=None,
        )

    market.close_engagement(
        engagement_id=engagement_a.engagement_id,
        status="rejected",
        source_result_id="result:seller-reject",
    )
    request_b = market.submit_request(
        _request("request_b", buyer.intent_id, seller_b.intent_id)
    )
    engagement_b = market.engage_request(
        actor_participant_id="seller_b",
        request_id=request_b.request_id,
        selection_result_id="result:select-b",
    )
    assert engagement_b.buyer_participant_id == "buyer"


def test_deal_requires_opposite_agent_proposal_and_acceptance_results() -> None:
    market = AgentDrivenMarket()
    buyer = market.publish_intent(_intent("buyer", "buyer", "buy"))
    seller = market.publish_intent(
        _intent(
            "seller",
            "seller",
            "sell",
            public_price="1.9",
            limit_price="1.6",
        )
    )
    request = market.submit_request(_request("request_1", buyer.intent_id, seller.intent_id))
    engagement = market.engage_request(
        actor_participant_id="seller",
        request_id=request.request_id,
        selection_result_id="result:seller-engage",
    )

    with pytest.raises(AgentMarketError, match="opposite counterparties"):
        market.freeze_deal(
            engagement_id=engagement.engagement_id,
            latest_proposal_result_id="result:buyer-propose",
            latest_proposal_actor_id="buyer",
            acceptance_result_id="result:buyer-accept",
            accepted_by_participant_id="buyer",
            unit_price_atomic=gold("1.8"),
        )
    with pytest.raises(AgentMarketError, match="private hard limit"):
        market.freeze_deal(
            engagement_id=engagement.engagement_id,
            latest_proposal_result_id="result:buyer-propose",
            latest_proposal_actor_id="buyer",
            acceptance_result_id="result:seller-accept",
            accepted_by_participant_id="seller",
            unit_price_atomic=gold("1.5"),
        )

    deal = market.freeze_deal(
        engagement_id=engagement.engagement_id,
        latest_proposal_result_id="result:buyer-propose",
        latest_proposal_actor_id="buyer",
        acceptance_result_id="result:seller-accept",
        accepted_by_participant_id="seller",
        unit_price_atomic=gold("1.8"),
    )
    assert deal.request_id == request.request_id
    assert deal.latest_proposal_result_id == "result:buyer-propose"
    assert deal.acceptance_result_id == "result:seller-accept"
    assert (
        market.freeze_deal(
            engagement_id=engagement.engagement_id,
            latest_proposal_result_id="result:buyer-propose",
            latest_proposal_actor_id="buyer",
            acceptance_result_id="result:seller-accept",
            accepted_by_participant_id="seller",
            unit_price_atomic=gold("1.8"),
        )
        == deal
    )


def test_seller_can_accept_the_binding_rfq_opening_proposal() -> None:
    market = AgentDrivenMarket()
    buyer = market.publish_intent(_intent("buyer", "buyer", "buy"))
    seller = market.publish_intent(
        _intent(
            "seller",
            "seller",
            "sell",
            public_price="1.9",
            limit_price="1.6",
        )
    )
    request = market.submit_request(
        _request(
            "request_1",
            buyer.intent_id,
            seller.intent_id,
            opening_price="1.8",
        )
    )
    engagement = market.engage_request(
        actor_participant_id="seller",
        request_id=request.request_id,
        selection_result_id="result:seller-engage",
    )

    deal = market.freeze_deal(
        engagement_id=engagement.engagement_id,
        latest_proposal_result_id=request.source_result_id,
        latest_proposal_actor_id="buyer",
        acceptance_result_id="result:seller-accept",
        accepted_by_participant_id="seller",
        unit_price_atomic=request.opening_price_atomic,
    )

    assert deal.request_id == request.request_id
    assert deal.latest_proposal_result_id == request.source_result_id
    assert deal.unit_price_atomic == request.opening_price_atomic


def test_protocol_oracle_has_no_central_pairing_operation() -> None:
    market = AgentDrivenMarket()

    assert not hasattr(market, "match")
    assert not hasattr(market, "pair")
    with pytest.raises(AgentMarketError, match="request not found"):
        market.engage_request(
            actor_participant_id="seller",
            request_id="missing",
            selection_result_id="result:seller-engage",
        )


def test_one_result_cannot_author_two_market_actions() -> None:
    market = AgentDrivenMarket()
    buyer = market.publish_intent(_intent("buyer", "buyer", "buy"))
    seller = market.publish_intent(
        _intent(
            "seller",
            "seller",
            "sell",
            public_price="1.9",
            limit_price="1.6",
        )
    )
    request = _request("request_1", buyer.intent_id, seller.intent_id)
    request = AgentNegotiationRequest(
        request_id=request.request_id,
        source_result_id=buyer.source_result_id,
        buyer_intent_id=request.buyer_intent_id,
        seller_intent_id=request.seller_intent_id,
        opening_price_atomic=request.opening_price_atomic,
        public_message=request.public_message,
        created_at=request.created_at,
    )

    with pytest.raises(AgentMarketError, match="already authored"):
        market.submit_request(request)
