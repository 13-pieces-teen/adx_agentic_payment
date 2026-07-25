from __future__ import annotations

from decimal import Decimal

import pytest

from arena_game import (
    INITIAL_PRICES,
    GameConfig,
    GameError,
    GamePhase,
    MoneyError,
    PawnhouseGame,
    Portfolio,
    PortfolioError,
    RoundPhase,
    WorldState,
    apply_basis_points,
    build_event_schedule,
    demo_events,
    format_gold,
    gold,
    schedule_commitment,
)


def test_gold_is_fixed_point_and_rejects_float() -> None:
    assert gold("2") == 2_000_000
    assert gold(2) == 2_000_000
    assert gold(Decimal("2.123456")) == 2_123_456
    assert format_gold(2_123_400) == "2.1234"
    assert apply_basis_points(gold("2"), 2_500) == gold("2.5")

    with pytest.raises(MoneyError):
        gold(2.1)
    with pytest.raises(MoneyError):
        gold("0.0000001")


def test_initial_portfolio_must_equal_exactly_twenty_gold() -> None:
    portfolio = Portfolio.initial(
        cash_atomic=gold("0"),
        holdings={
            "grain": 2,
            "iron": 1,
            "warhorse": 1,
            "gems": 1,
        },
    )
    assert portfolio.net_worth(INITIAL_PRICES) == gold("20")

    with pytest.raises(PortfolioError, match="exactly 20"):
        Portfolio.initial(
            cash_atomic=gold("1"),
            holdings={"grain": 2, "iron": 1, "warhorse": 1, "gems": 1},
        )
    with pytest.raises(PortfolioError, match="unknown goods"):
        Portfolio.initial(
            cash_atomic=gold("20"),
            holdings={"silk": 1},
        )


def test_demo_event_schedule_is_deterministic_and_resets_gem_bubble() -> None:
    events = demo_events()
    commitment = schedule_commitment(events, seed="fixed-demo-seed")
    assert commitment == schedule_commitment(events, seed="fixed-demo-seed")
    assert commitment != schedule_commitment(events, seed="different-seed")

    world = WorldState({event.event_id: event for event in events})
    round_one = world.reveal(events[0].event_id, round_index=1)
    assert round_one.market_prices["iron"] == gold("10")
    assert round_one.final_prices["iron"] == gold("5.5")
    assert round_one.royal_orders[0].price_atomic == gold("15")
    assert round_one.royal_orders[0].quantity_limit == 200

    round_two = world.reveal(events[1].event_id, round_index=2)
    assert round_two.market_prices["iron"] == INITIAL_PRICES["iron"]
    assert round_two.bubble_premium_bps["gems"] == 2_500
    assert round_two.market_prices["gems"] == gold("3.75")

    world.reveal(events[2].event_id, round_index=3)
    world.reveal(events[3].event_id, round_index=4)
    round_five = world.reveal(events[4].event_id, round_index=5)
    assert round_five.bubble_premium_bps["gems"] == 0
    assert round_five.market_prices["gems"] == INITIAL_PRICES["gems"]
    assert round_five.final_prices["gems"] == INITIAL_PRICES["gems"]


def test_seeded_event_deck_builds_a_replayable_ten_round_schedule() -> None:
    first = build_event_schedule(
        round_count=10,
        seed="ten-round-seed",
        mode="seeded_shuffle",
    )
    replay = build_event_schedule(
        round_count=10,
        seed="ten-round-seed",
        mode="seeded_shuffle",
    )
    other = build_event_schedule(
        round_count=10,
        seed="different-ten-round-seed",
        mode="seeded_shuffle",
    )

    assert [event.to_wire() for event in first] == [
        event.to_wire() for event in replay
    ]
    assert [event.event_id for event in first] != [
        event.event_id for event in other
    ]
    assert [event.reveal_round for event in first] == list(range(1, 11))
    assert len({event.event_id for event in first}) == 10


def test_game_requires_unique_users_and_locked_twenty_gold_portfolios() -> None:
    game = PawnhouseGame(
        game_id="game_demo",
        config=GameConfig(),
        events=demo_events(),
        event_seed="fixed-demo-seed",
    )
    game.join(user_id="user_a", agent_id="agent_a")
    game.join(user_id="user_b", agent_id="agent_b")
    with pytest.raises(GameError, match="one user"):
        game.join(user_id="user_a", agent_id="agent_c")

    balanced = Portfolio.initial(
        cash_atomic=gold("0"),
        holdings={"grain": 2, "iron": 1, "warhorse": 1, "gems": 1},
    )
    cash_only = Portfolio.initial(cash_atomic=gold("20"), holdings={})
    game.configure_portfolio(agent_id="agent_a", portfolio=balanced)
    with pytest.raises(GameError, match="every participant"):
        game.lock_portfolios()
    game.configure_portfolio(agent_id="agent_b", portfolio=cash_only)
    game.lock_portfolios()

    first_snapshot = game.start()
    assert game.phase is GamePhase.RUNNING
    assert game.round_phase is RoundPhase.DECIDE
    assert first_snapshot.revealed_event_ids == ("palace-requisition",)


def test_game_advances_all_rounds_and_calculates_deterministic_ranking() -> None:
    game = PawnhouseGame(
        game_id="game_rank",
        config=GameConfig(),
        events=demo_events(),
        event_seed="fixed-demo-seed",
    )
    game.join(user_id="user_a", agent_id="agent_a")
    game.join(user_id="user_b", agent_id="agent_b")
    game.configure_portfolio(
        agent_id="agent_a",
        portfolio=Portfolio.initial(
            cash_atomic=gold("0"),
            holdings={"grain": 10},
        ),
    )
    game.configure_portfolio(
        agent_id="agent_b",
        portfolio=Portfolio.initial(cash_atomic=gold("20"), holdings={}),
    )
    game.lock_portfolios()
    game.start()

    transition = (
        (RoundPhase.DECIDE, RoundPhase.MATCH),
        (RoundPhase.MATCH, RoundPhase.NEGOTIATE),
        (RoundPhase.NEGOTIATE, RoundPhase.SETTLE),
        (RoundPhase.SETTLE, RoundPhase.ROUND_CLOSE),
        (RoundPhase.ROUND_CLOSE, RoundPhase.COMPLETED),
    )
    for round_index in range(1, game.config.round_count + 1):
        assert game.current_round == round_index
        for expected, target in transition:
            game.move_round_phase(expected, target)
        game.next_round()

    assert game.phase is GamePhase.FINAL_VALUATION
    rankings = game.complete()
    assert game.phase is GamePhase.COMPLETED
    assert rankings[0].agent_id == "agent_a"
    assert rankings[0].tier == "公爵"
    assert rankings[0].net_worth_atomic > rankings[1].net_worth_atomic
