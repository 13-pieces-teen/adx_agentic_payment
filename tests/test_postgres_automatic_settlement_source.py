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

    async def execute(self, query: str, *args: object) -> str:
        self.calls.append((query, args))
        return "DELETE 1"


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


def test_facilitator_fence_claim_atomically_blocks_unresolved_broadcasts() -> None:
    pool = _Pool()
    source = PostgresAutomaticSettlementSource(
        payments=_Payments(pool),  # type: ignore[arg-type]
        arena=object(),  # type: ignore[arg-type]
        public_api_url="https://api.example.test",
    )
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)

    claimed = asyncio.run(
        source.claim_facilitator_fence(
            facilitator_id="shard-3",
            settlement_intent_id="intent-1",
            worker_id="worker-1",
            now=now,
        )
    )

    query, args = pool.calls[0]
    assert claimed is True
    assert "INSERT INTO arena402.facilitator_broadcast_fences" in query
    assert "attempt.status IN ('submitting', 'unknown')" in query
    assert "submission.settlement_intent_id IS NULL" in query
    assert "fence.lease_expires_at < $5" in query
    assert args[:3] == ("shard-3", "intent-1", "worker-1")


def test_facilitator_fence_release_is_owner_and_intent_scoped() -> None:
    pool = _Pool()
    source = PostgresAutomaticSettlementSource(
        payments=_Payments(pool),  # type: ignore[arg-type]
        arena=object(),  # type: ignore[arg-type]
        public_api_url="https://api.example.test",
    )

    asyncio.run(
        source.release_facilitator_fence(
            facilitator_id="shard-3",
            settlement_intent_id="intent-1",
            worker_id="worker-1",
        )
    )

    query, args = pool.calls[0]
    assert "DELETE FROM arena402.facilitator_broadcast_fences" in query
    assert "facilitator_id = $1" in query
    assert "settlement_intent_id = $2" in query
    assert "lease_owner = $3" in query
    assert args == ("shard-3", "intent-1", "worker-1")


def test_busy_facilitator_defers_only_the_owned_pre_submission_attempt() -> None:
    class _UpdatePool(_Pool):
        async def execute(self, query: str, *args: object) -> str:
            self.calls.append((query, args))
            return "UPDATE 1"

    pool = _UpdatePool()
    source = PostgresAutomaticSettlementSource(
        payments=_Payments(pool),  # type: ignore[arg-type]
        arena=object(),  # type: ignore[arg-type]
        public_api_url="https://api.example.test",
    )
    retry_at = datetime(2026, 7, 26, 0, 0, 1, tzinfo=timezone.utc)

    asyncio.run(
        source.defer_attempt(
            settlement_intent_id="intent-1",
            worker_id="worker-1",
            retry_at=retry_at,
        )
    )

    query, args = pool.calls[0]
    assert "lease_expires_at = $3" in query
    assert "facilitator_deferred_at = clock_timestamp()" in query
    assert "facilitator_defer_count = facilitator_defer_count + 1" in query
    assert "lease_owner = $2" in query
    assert "status IN ('reserved', 'signed')" in query
    assert args == ("intent-1", "worker-1", retry_at)


def test_attempt_status_updates_capture_each_submission_stage_timestamp() -> None:
    class _UpdatePool(_Pool):
        async def execute(self, query: str, *args: object) -> str:
            self.calls.append((query, args))
            return "UPDATE 1"

    pool = _UpdatePool()
    source = PostgresAutomaticSettlementSource(
        payments=_Payments(pool),  # type: ignore[arg-type]
        arena=object(),  # type: ignore[arg-type]
        public_api_url="https://api.example.test",
    )

    asyncio.run(
        source.mark_attempt(
            settlement_intent_id="intent-1",
            status="signed",
            worker_id="worker-1",
            payment_payload_digest="sha256:" + "11" * 32,
        )
    )

    query, _ = pool.calls[0]
    assert "signed_at = CASE" in query
    assert "submitting_at = CASE" in query
    assert "submitted_at = CASE" in query
    assert "COALESCE(signed_at, clock_timestamp())" in query
    assert "COALESCE(submitting_at, clock_timestamp())" in query
    assert "COALESCE(submitted_at, clock_timestamp())" in query


def test_recovered_submission_clears_the_unresolved_facilitator_fence() -> None:
    class _RecoveryPool(_Pool):
        async def fetchrow(
            self,
            query: str,
            *args: object,
        ) -> dict[str, str]:
            self.calls.append((query, args))
            return {
                "reservation_id": "reservation-1",
                "facilitator_id": "shard-3",
            }

    class _RecoveryPayments(_Payments):
        async def submit_reservation(self, *_: object, **__: object) -> None:
            return None

    class _RecoveryArena:
        async def settlement_intent_for_payment(self, **_: object):
            return type(
                "Intent",
                (),
                {
                    "settlement_intent_id": "intent-1",
                    "intent_hash": "sha256:" + "11" * 32,
                    "game_id": "game-1",
                    "buyer_account": "0x" + "22" * 20,
                    "seller_account": "0x" + "33" * 20,
                    "chain_id": 1439,
                    "token_address": "0x" + "44" * 20,
                    "token_symbol": "arena402-g",
                    "token_decimals": 6,
                    "token_eip712_name": "Arena402 Game Coin",
                    "token_eip712_version": "1",
                    "amount_atomic": 2_500_000,
                },
            )()

        async def record_automatic_submission(self, **_: object) -> None:
            return None

    pool = _RecoveryPool()
    source = PostgresAutomaticSettlementSource(
        payments=_RecoveryPayments(pool),  # type: ignore[arg-type]
        arena=_RecoveryArena(),  # type: ignore[arg-type]
        public_api_url="https://api.example.test",
    )

    asyncio.run(
        source.record_recovered_submission(
            settlement_intent_id="intent-1",
            tx_hash="0x" + "55" * 32,
            now=datetime(2026, 7, 26, tzinfo=timezone.utc),
        )
    )

    assert "facilitator_id" in pool.calls[0][0]
    delete_query, delete_args = pool.calls[-1]
    assert "DELETE FROM arena402.facilitator_broadcast_fences" in delete_query
    assert delete_args == ("shard-3", "intent-1")


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
