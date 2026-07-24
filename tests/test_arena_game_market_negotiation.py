from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from arena_game import (
    MarketError,
    Negotiation,
    NegotiationAction,
    NegotiationError,
    NegotiationStatus,
    PoolEntry,
    RuleRuntime,
    RuleStrategy,
    fcfs_pair,
    gold,
)


def _entry(
    entry_id: str,
    participant_id: str,
    side: str,
    offset_ms: int,
    *,
    good: str = "iron",
) -> PoolEntry:
    return PoolEntry(
        pool_entry_id=entry_id,
        game_id="game_1",
        round_id="round_1",
        participant_id=participant_id,
        side=side,  # type: ignore[arg-type]
        good=good,  # type: ignore[arg-type]
        entered_at=datetime(2026, 7, 25, tzinfo=timezone.utc)
        + timedelta(milliseconds=offset_ms),
    )


def test_fcfs_uses_authoritative_time_then_stable_entry_id() -> None:
    pairings = fcfs_pair(
        (
            _entry("buyer_2", "participant_b2", "buy", 20),
            _entry("seller_2", "participant_s2", "sell", 10),
            _entry("buyer_1", "participant_b1", "buy", 10),
            _entry("seller_1", "participant_s1", "sell", 5),
        )
    )
    assert [
        (
            pairing.buyer_participant_id,
            pairing.seller_participant_id,
        )
        for pairing in pairings
    ] == [
        ("participant_b1", "participant_s1"),
        ("participant_b2", "participant_s2"),
    ]


def test_fcfs_rejects_cross_round_and_self_pairing() -> None:
    cross_round = _entry("seller", "seller", "sell", 1)
    object.__setattr__(cross_round, "round_id", "round_2")
    with pytest.raises(MarketError, match="one game round"):
        fcfs_pair((_entry("buyer", "buyer", "buy", 0), cross_round))

    with pytest.raises(MarketError, match="itself"):
        fcfs_pair(
            (
                _entry("buyer", "same", "buy", 0),
                _entry("seller", "same", "sell", 1),
            )
        )


def test_three_turn_negotiation_accepts_latest_counterparty_quote() -> None:
    negotiation = Negotiation(
        negotiation_id="neg_1",
        buyer_participant_id="buyer",
        seller_participant_id="seller",
    )
    negotiation.apply(
        role="buyer",
        action=NegotiationAction(
            action="propose",
            price_atomic=gold("6"),
            message="六金，今日便成交。",
        ),
    )
    negotiation.apply(
        role="seller",
        action=NegotiationAction(
            action="propose",
            price_atomic=gold("7"),
            message="七金，少一枚都不卖。",
        ),
    )
    negotiation.apply(
        role="buyer",
        action=NegotiationAction(action="accept"),
    )
    assert negotiation.status is NegotiationStatus.ACCEPTED_PENDING_SETTLEMENT
    assert negotiation.accepted_price_atomic == gold("7")


def test_negotiation_enforces_order_shape_and_last_turn() -> None:
    negotiation = Negotiation(
        negotiation_id="neg_2",
        buyer_participant_id="buyer",
        seller_participant_id="seller",
    )
    with pytest.raises(NegotiationError, match="open with"):
        negotiation.apply(
            role="buyer",
            action=NegotiationAction(action="reject", message="不买。"),
        )
    negotiation.apply(
        role="buyer",
        action=NegotiationAction(
            action="propose",
            price_atomic=gold("6"),
            message="六金。",
        ),
    )
    with pytest.raises(NegotiationError, match="out of turn"):
        negotiation.apply(
            role="buyer",
            action=NegotiationAction(action="accept"),
        )
    negotiation.apply(
        role="seller",
        action=NegotiationAction(
            action="propose",
            price_atomic=gold("7"),
            message="七金。",
        ),
    )
    with pytest.raises(NegotiationError, match="last negotiation turn"):
        negotiation.apply(
            role="buyer",
            action=NegotiationAction(
                action="propose",
                price_atomic=gold("6.5"),
                message="六金半。",
            ),
        )


def test_rule_runtime_can_complete_a_deterministic_negotiation() -> None:
    buyer = RuleRuntime(
        RuleStrategy(
            intent="buy",
            good="iron",
            target_price_atomic=gold("7"),
            public_message="七金以内，愿当场成交。",
        )
    )
    seller = RuleRuntime(
        RuleStrategy(
            intent="sell",
            good="iron",
            target_price_atomic=gold("6"),
            public_message="六金即可交货。",
        )
    )
    negotiation = Negotiation(
        negotiation_id="neg_rule",
        buyer_participant_id="buyer",
        seller_participant_id="seller",
    )
    buyer_offer = buyer.negotiate(
        role="buyer",
        sequence=1,
        latest_counterparty_price_atomic=None,
        max_turns=3,
    )
    negotiation.apply(role="buyer", action=buyer_offer)
    seller_response = seller.negotiate(
        role="seller",
        sequence=2,
        latest_counterparty_price_atomic=buyer_offer.price_atomic,
        max_turns=3,
    )
    negotiation.apply(role="seller", action=seller_response)
    assert negotiation.status is NegotiationStatus.ACCEPTED_PENDING_SETTLEMENT
    assert negotiation.accepted_price_atomic == gold("7")

