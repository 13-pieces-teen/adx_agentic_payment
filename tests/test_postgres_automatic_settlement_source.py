from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arena_game.postgres import PostgresPawnhouseRepository
from arena_payments.postgres_worker import PostgresAutomaticSettlementSource


class _Pool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, query: str, *args: object) -> list[dict[str, str]]:
        self.calls.append((query, args))
        return []

    async def fetchrow(
        self,
        query: str,
        *args: object,
    ) -> dict[str, str]:
        self.calls.append((query, args))
        return {"settlement_intent_id": str(args[0])}


class _Payments:
    def __init__(self, pool: _Pool) -> None:
        self.pool = pool

    def _require_pool(self) -> _Pool:
        return self.pool


def test_source_scopes_claim_queries_to_one_explicit_intent() -> None:
    pool = _Pool()
    target = "settlement:approved-intent"
    source = PostgresAutomaticSettlementSource(
        payments=_Payments(pool),  # type: ignore[arg-type]
        arena=object(),  # type: ignore[arg-type]
        public_api_url="https://api.example.test",
        settlement_intent_id=target,
    )

    asyncio.run(source.authorization_targets(limit=3))
    asyncio.run(source.unknown_submission_targets(limit=4))

    assert [call[1] for call in pool.calls] == [(3, target), (4, target)]
    assert all(
        "intent.settlement_intent_id = $2::text" in call[0]
        for call in pool.calls
    )


def test_source_rejects_invalid_explicit_intent_scope() -> None:
    with pytest.raises(ValueError, match="invalid_automatic_settlement_intent_id"):
        PostgresAutomaticSettlementSource(
            payments=object(),  # type: ignore[arg-type]
            arena=object(),  # type: ignore[arg-type]
            public_api_url="https://api.example.test",
            settlement_intent_id="x" * 513,
        )


def test_claim_persists_the_selected_facilitator_shard() -> None:
    pool = _Pool()
    source = PostgresAutomaticSettlementSource(
        payments=_Payments(pool),  # type: ignore[arg-type]
        arena=object(),  # type: ignore[arg-type]
        public_api_url="https://api.example.test",
    )

    claimed = asyncio.run(
        source.claim_attempt(
            settlement_intent_id="intent-1",
            reservation_id="reservation-1",
            payment_required={
                "accepts": [{"network": "eip155:1439"}]
            },
            facilitator_id="shard-3",
            worker_id="worker-1",
            now=datetime(2026, 7, 26, tzinfo=timezone.utc),
        )
    )

    query, args = pool.calls[0]
    assert claimed is True
    assert "facilitator_id" in query
    assert "EXCLUDED.facilitator_id" in query
    assert args[4] == "shard-3"


def test_settlement_repository_uses_least_privilege_database_role() -> None:
    commands: list[str] = []

    class _Connection:
        async def execute(self, command: str) -> None:
            commands.append(command)

    repository = PostgresPawnhouseRepository(
        "",
        pool=object(),
        database_role="adx_settlement",
    )
    asyncio.run(repository._setup_connection(_Connection()))

    assert commands == [
        "SET ROLE adx_settlement",
        "SET search_path TO pg_catalog, arena402, public",
    ]


def test_mandate_reservation_does_not_lock_read_only_balance_table() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "arena_payments" / "postgres.py"
    ).read_text(encoding="utf-8")
    balance_query = """SELECT cash_atomic
                    FROM arena402.balances
                    WHERE game_participant_id = $1"""

    assert balance_query in source
    assert f"{balance_query}\n                    FOR UPDATE" not in source
