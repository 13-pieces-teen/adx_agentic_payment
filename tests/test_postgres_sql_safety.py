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
    source = inspect.getsource(
        PostgresPawnhouseRepository.add_hosted_participant
    )

    assert "official-mandate:" in source
    assert "official-ja:" in source
    assert "'same_game_settlement_account'" in source
    assert "initialNetWorthAtomic" in source
    assert "roundCount" in source
    assert "official_mandate_not_allowed" in source


def test_settlement_resolves_both_user_and_official_wallet_authorities() -> None:
    sources = (
        inspect.getsource(
            PostgresPaymentRepository.active_mandate_for_settlement
        ),
        inspect.getsource(PostgresPaymentRepository.reserve_mandate),
    )

    for source in sources:
        assert "payment_wallet_authorities" in source
        assert "wallet_inventory" in source
        assert "wallet.status <> 'disabled'" in source
