from __future__ import annotations

from decimal import Decimal

import pytest

from arena_game import (
    EffectKind,
    EXPERIMENTAL_EVENT_DECK_ID_V2,
    EXPERIMENTAL_PRICE_CATALOG_ID_V2,
    EventEffect,
    INITIAL_PRICES,
    GameConfig,
    GameError,
    GamePhase,
    MoneyError,
    PawnhouseGame,
    Portfolio,
    PortfolioError,
    RoundPhase,
    STANDARD_PRICE_CATALOG_ID,
    WorldEvent,
    WorldState,
    apply_market_feedback,
    apply_basis_points,
    build_event_schedule,
    demo_events,
    format_gold,
    gold,
    price_catalog_from_snapshot,
    resolve_price_catalog,
    schedule_commitment,
)
from arena_game.portfolio import (
    default_join_portfolio,
    distribute_balanced_portfolios,
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
    assert round_one.royal_orders == ()

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


def test_world_reset_uses_the_game_frozen_base_prices() -> None:
    event = WorldEvent(
        event_id="reset-gems",
        display_name="价格重置",
        narrative="测试冻结基础价格。",
        reveal_round=1,
        duration_rounds=None,
        effects=(
            EventEffect(
                kind=EffectKind.PRICE_RESET_TO_BASE,
                good="gems",
                target="both",
            ),
        ),
    )
    frozen_prices = {
        "grain": gold("2.5"),
        "iron": gold("4.5"),
        "warhorse": gold("7"),
        "gems": gold("4"),
    }

    snapshot = WorldState(
        {event.event_id: event},
        base_prices=frozen_prices,
    ).reveal(event.event_id, round_index=1)

    assert snapshot.market_prices == frozen_prices
    assert snapshot.final_prices == frozen_prices


def test_standard_price_catalog_freezes_the_existing_mvp_prices() -> None:
    catalog = resolve_price_catalog(STANDARD_PRICE_CATALOG_ID)

    assert catalog.catalog_id == "pawnhouse-price-v1"
    assert catalog.prices == INITIAL_PRICES
    assert catalog.to_snapshot() == {
        "priceCatalogId": "pawnhouse-price-v1",
        "initialPricesAtomic": {
            "grain": "2000000",
            "iron": "5000000",
            "warhorse": "8000000",
            "gems": "3000000",
        },
    }

    with pytest.raises(ValueError, match="unknown price catalog"):
        resolve_price_catalog("unknown-price-catalog")


def test_experimental_v2_price_catalog_compresses_unit_ticket_size() -> None:
    catalog = resolve_price_catalog(EXPERIMENTAL_PRICE_CATALOG_ID_V2)

    assert catalog.catalog_id == "pawnhouse-price-v2"
    assert catalog.prices == {
        "grain": gold("2.5"),
        "iron": gold("4"),
        "warhorse": gold("6"),
        "gems": gold("3"),
    }
    assert max(catalog.prices.values()) * 10 <= (
        min(catalog.prices.values()) * 24
    )


def test_frozen_price_snapshot_replays_without_the_live_catalog() -> None:
    catalog = price_catalog_from_snapshot(
        {
            "priceCatalogId": "pawnhouse-price-v2",
            "initialPricesAtomic": {
                "grain": "2500000",
                "iron": "4500000",
                "warhorse": "7000000",
                "gems": "4000000",
            },
        }
    )

    assert catalog.catalog_id == "pawnhouse-price-v2"
    assert catalog.prices["gems"] == gold("4")


def test_frozen_price_snapshot_rejects_non_atomic_numbers() -> None:
    with pytest.raises(ValueError, match="frozen initial price"):
        price_catalog_from_snapshot(
            {
                "priceCatalogId": "pawnhouse-price-v2",
                "initialPricesAtomic": {
                    "grain": 2.5,
                    "iron": "4500000",
                    "warhorse": "7000000",
                    "gems": "4000000",
                },
            }
        )


def test_market_feedback_is_bounded_and_keeps_event_price_as_anchor() -> None:
    event = demo_events()[0]
    snapshot = WorldState({event.event_id: event}).reveal(
        event.event_id,
        round_index=1,
    )

    feedback = apply_market_feedback(
        snapshot,
        last_clearing_prices={"iron": gold("20")},
        buy_pressure_bps={"iron": 10_000},
    )

    assert snapshot.market_prices["iron"] == gold("10")
    assert feedback.market_prices["iron"] == gold("15")
    assert feedback.market_prices["grain"] == INITIAL_PRICES["grain"]


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


def test_standard_v1_event_deck_preserves_the_historical_seed_order() -> None:
    schedule = build_event_schedule(
        round_count=8,
        seed="phase-d5a-seed-01",
        mode="seeded_shuffle",
    )

    assert [event.event_id for event in schedule] == [
        "coronation-cancelled",
        "royal-wedding",
        "stable-plague",
        "barbarian-siege",
        "peace-rumor",
        "noble-gem-fever",
        "new-iron-mine",
        "granary-fire",
    ]


def test_experimental_v2_event_deck_has_bounded_two_sided_shocks() -> None:
    schedule = build_event_schedule(
        round_count=10,
        seed="market-quality-v2-seed",
        deck_id=EXPERIMENTAL_EVENT_DECK_ID_V2,
        mode="seeded_shuffle",
    )

    market_only_signs = {
        good: set()
        for good in ("grain", "iron", "warhorse", "gems")
    }
    for event in schedule:
        assert event.event_id.endswith("-v2")
        for effect in event.effects:
            if effect.kind is EffectKind.PRICE_MULTIPLY_BPS:
                assert effect.basis_points is not None
                assert abs(effect.basis_points) <= 1_000
                if effect.target == "market":
                    market_only_signs[effect.good].add(
                        1 if effect.basis_points > 0 else -1
                    )

    assert market_only_signs == {
        "grain": {-1, 1},
        "iron": {-1, 1},
        "warhorse": {-1, 1},
        "gems": {-1, 1},
    }


def test_game_config_accepts_two_hundred_participants() -> None:
    config = GameConfig(max_participants=200)

    assert config.max_participants == 200


def test_balanced_auto_portfolios_are_deterministic_and_equal_value() -> None:
    first = distribute_balanced_portfolios(
        ("agent_a", "agent_b", "agent_c"),
        seed="portfolio-seed",
    )
    replay = distribute_balanced_portfolios(
        ("agent_a", "agent_b", "agent_c"),
        seed="portfolio-seed",
    )

    assert first == replay
    assert all(
        portfolio.net_worth(INITIAL_PRICES) == gold("20")
        for portfolio in first.values()
    )
    assert all(sum(portfolio.holdings.values()) == 1 for portfolio in first.values())


def test_default_join_portfolio_is_deterministic_equal_value_and_sell_capable() -> None:
    first = default_join_portfolio(
        game_id="game-current",
        agent_id="agent-current",
    )
    replay = default_join_portfolio(
        game_id="game-current",
        agent_id="agent-current",
    )

    assert first == replay
    assert first.net_worth(INITIAL_PRICES) == gold("20")
    assert sum(first.holdings.values()) == 1
    assert first.cash_atomic < gold("20")


def test_game_balanced_auto_mode_assigns_missing_portfolios_at_lock() -> None:
    game = PawnhouseGame(
        game_id="game_auto_portfolio",
        config=GameConfig(portfolio_mode="balanced_auto"),
        events=demo_events(),
        event_seed="portfolio-seed",
    )
    game.join(user_id="user_a", agent_id="agent_a")
    game.join(user_id="user_b", agent_id="agent_b")

    game.lock_portfolios()

    assert set(game.portfolio_snapshot()) == {"agent_a", "agent_b"}
    assert all(
        portfolio.net_worth(INITIAL_PRICES) == gold("20")
        for portfolio in game.portfolio_snapshot().values()
    )


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
