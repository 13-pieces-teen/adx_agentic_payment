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
