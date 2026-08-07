from __future__ import annotations

import inspect
import re

from arena_game.postgres import PostgresPawnhouseRepository
from arena_payments.postgres import PostgresPaymentRepository


def test_join_queries_do_not_use_authorization_as_a_postgres_alias() -> None:
    sources = (
        inspect.getsource(PostgresPaymentRepository.create_mandate),
        inspect.getsource(PostgresPawnhouseRepository.add_hosted_participant),
    )

    for source in sources:
        assert re.search(r"\bAS\s+authorization\b", source, re.IGNORECASE) is None
        assert "AS join_auth" in source


def test_official_agents_receive_bounded_game_scoped_payment_authority() -> None:
    source = inspect.getsource(PostgresPawnhouseRepository.add_hosted_participant)

    assert "official-mandate:" in source
    assert "official-ja:" in source
    assert "'same_game_settlement_account'" in source
    assert "initialNetWorthAtomic" in source
    assert "roundCount" in source
    assert "official_mandate_not_allowed" in source


def test_settlement_resolves_both_user_and_official_wallet_authorities() -> None:
    sources = (
        inspect.getsource(PostgresPaymentRepository.active_mandate_for_settlement),
        inspect.getsource(PostgresPaymentRepository.reserve_mandate),
    )

    for source in sources:
        assert "payment_wallet_authorities" in source
        assert "wallet_inventory" in source
        assert "wallet.status <> 'disabled'" in source


def test_game_start_and_final_ranking_exclude_pending_participants() -> None:
    start_source = inspect.getsource(PostgresPawnhouseRepository._start_game_locked)
    finalize_source = inspect.getsource(PostgresPawnhouseRepository._finalize_game)

    assert "readiness = 'ready'" in start_source
    assert "SET status = 'cancelled'" in start_source
    assert "SET status = 'active'" in start_source
    assert "AND readiness = 'ready'" in finalize_source


def test_initial_round_start_is_written_to_the_public_event_ledger() -> None:
    start_source = inspect.getsource(
        PostgresPawnhouseRepository._start_game_locked
    )

    event_index = start_source.index('event_type="round.started"')
    world_index = start_source.index("_persist_world_snapshot")
    assert event_index < world_index
    assert 'source_key=f"{round_id}:started"' in start_source


def test_no_payment_participants_become_ready_without_a_mandate() -> None:
    sources = (
        inspect.getsource(PostgresPawnhouseRepository.add_hosted_participant),
        inspect.getsource(PostgresPawnhouseRepository.add_connector_participant),
    )

    for source in sources:
        assert "ready_without_payment" in source
        assert 'settlement_config.authorization_mode == "none"' in source
        assert "portfolio_locked_at" in source
        assert "readiness" in source
        assert "ready_at" in source


def test_connector_current_game_join_uses_the_same_mandate_boundary() -> None:
    dispatch_source = inspect.getsource(
        PostgresPawnhouseRepository.add_current_participant
    )
    connector_source = inspect.getsource(
        PostgresPawnhouseRepository.add_connector_participant
    )
    preflight_source = inspect.getsource(
        PostgresPawnhouseRepository.current_game_join_preflight
    )

    assert 'runtime_kind == "hosted"' in dispatch_source
    assert 'runtime_kind == "connector"' in dispatch_source
    assert "payment_mandate_id" in connector_source
    assert "'same_game_settlement_account'" in connector_source
    assert "join_authorization_id" in connector_source
    assert "game_coin_provisions" in connector_source
    assert "resolve_connector_binding_for_arena" in preflight_source


def test_settlement_terminal_state_updates_pairing_and_negotiation() -> None:
    commit_source = inspect.getsource(
        PostgresPawnhouseRepository.commit_confirmed_inventory
    )
    automatic_failure_source = inspect.getsource(
        PostgresPawnhouseRepository.record_automatic_failure
    )
    reverted_source = inspect.getsource(
        PostgresPawnhouseRepository.record_chain_reverted
    )

    assert "UPDATE arena402.negotiations" in commit_source
    assert "SET status = 'settled'" in commit_source
    for source in (automatic_failure_source, reverted_source):
        assert "UPDATE arena402.negotiations" in source
        assert "SET status = 'settlement_failed'" in source


def test_each_nonterminal_negotiation_turn_refreshes_its_action_deadline() -> None:
    source = inspect.getsource(
        PostgresPawnhouseRepository.apply_hosted_negotiation_action
    )

    assert "action_deadline_at = CASE" in source
    assert "SELECT action_timeout_ms" in source
    assert "clock_timestamp()" in source
