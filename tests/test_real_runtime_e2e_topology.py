import pytest

from tests.real_runtimes_docker_e2e import (
    assert_terminal_agent_market,
    game_create_payload,
    portfolio_for_seat,
    resolve_topology,
)


def test_ten_agent_codex_wave_has_equal_value_buyer_and_seller_seats() -> None:
    topology = resolve_topology(
        invites=[f"invite-{index}" for index in range(10)],
        runtime_kinds=["codex"] * 10,
        buyer_seats=range(5),
    )

    assert topology.participant_count == 10
    assert topology.buyer_seats == frozenset(range(5))
    assert all(kind == "codex" for kind in topology.runtime_kinds)
    assert [portfolio_for_seat(index, topology.buyer_seats) for index in range(10)] == [
        *[{"cash": "20.000000", "holdings": {}} for _ in range(5)],
        *[{"cash": "0.000000", "holdings": {"grain": 10}} for _ in range(5)],
    ]


def test_ten_agent_game_payload_uses_supported_capacity_contract() -> None:
    payload = game_create_payload(
        game_id="real-runtimes-load",
        event_seed="load-seed",
        participant_count=10,
        action_timeout_ms=300_000,
        round_count=1,
        market_protocol="agent_a2a.v1",
    )

    assert payload["maxParticipants"] == 10
    assert "minParticipants" not in payload
    assert payload["settlement"] == {"authorizationMode": "none"}


def test_diversified_seller_portfolios_keep_equal_initial_net_worth() -> None:
    buyers = frozenset(range(5))

    assert [
        portfolio_for_seat(5 + index, buyers, seller_good=good)
        for index, good in enumerate(("grain", "iron", "warhorse", "gems"))
    ] == [
        {"cash": "0.000000", "holdings": {"grain": 10}},
        {"cash": "0.000000", "holdings": {"iron": 4}},
        {"cash": "4.000000", "holdings": {"warhorse": 2}},
        {"cash": "2.000000", "holdings": {"gems": 6}},
    ]


def test_completed_a2a_game_evidence_rejects_residual_market_state() -> None:
    clean = {
        "nonterminal_market_intents": 0,
        "pending_market_requests": 0,
        "active_market_sessions": 0,
        "reserved_market_slots": 0,
    }
    assert_terminal_agent_market(clean)

    dirty = {**clean, "pending_market_requests": 1}
    with pytest.raises(
        RuntimeError,
        match="completed A2A game retained nonterminal market state",
    ):
        assert_terminal_agent_market(dirty)
