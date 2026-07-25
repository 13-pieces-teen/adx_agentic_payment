from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from arena_game.postgres import PostgresPawnhouseRepository


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db" / "migrations" / "030_arena_public_trade_ledger.sql"


class _Pool:
    def __init__(self) -> None:
        self.fetch_args: tuple[object, ...] | None = None
        self.fetchrow_query = ""

    async def fetch(self, query: str, *args: object):
        self.fetch_args = args
        return [
            {
                "settlement_intent_id": "trade-1",
                "game_id": "game-1",
                "round_index": 2,
                "good_id": "iron",
                "quantity": 3,
                "unit_price_atomic": 7_000_000,
                "amount_atomic": 21_000_000,
                "buyer_agent_id": "buyer-1",
                "buyer_display_name": "Buyer One",
                "buyer_account": "0x" + "11" * 20,
                "seller_agent_id": "seller-1",
                "seller_display_name": "Seller One",
                "seller_account": "0x" + "22" * 20,
                "pairing_id": "pairing-1",
                "chain_id": 1439,
                "status": "inventory_committed",
                "created_at": datetime(2026, 7, 26, 1, 0, tzinfo=timezone.utc),
                "chain_confirmed_at": datetime(2026, 7, 26, 1, 1, tzinfo=timezone.utc),
                "tx_hash": "0x" + "33" * 32,
                "block_number": 100,
                "confirmation_observed_at": datetime(
                    2026, 7, 26, 1, 1, tzinfo=timezone.utc
                ),
                "facilitator_address": "0x" + "44" * 20,
            }
        ]

    async def fetchrow(self, query: str):
        self.fetchrow_query = query
        return {
            "trade_count": 7,
            "volume_atomic": 49_000_000,
            "agent_count": 4,
        }


def test_repository_projects_cross_game_trade_rows() -> None:
    pool = _Pool()
    repository = PostgresPawnhouseRepository("", pool=pool)
    cursor_time = datetime(2026, 7, 26, tzinfo=timezone.utc)

    values = asyncio.run(
        repository.ledger_trades(
            game_id="game-1",
            agent_id="buyer-1",
            good_id="iron",
            after_created_at=cursor_time,
            after_trade_id="trade-2",
            limit=11,
        )
    )

    assert pool.fetch_args == (
        "game-1",
        "buyer-1",
        "iron",
        cursor_time,
        "trade-2",
        11,
    )
    assert values[0]["tradeId"] == "trade-1"
    assert values[0]["round"] == 2
    assert values[0]["priceAtomic"] == "7000000"
    assert values[0]["amountAtomic"] == "21000000"
    assert values[0]["buyer"]["displayName"] == "Buyer One"
    assert values[0]["buyer"]["accountAddress"] == "0x" + "11" * 20
    assert values[0]["facilitatorAddress"] == "0x" + "44" * 20


def test_stats_count_only_rows_with_confirmation_evidence() -> None:
    pool = _Pool()
    repository = PostgresPawnhouseRepository("", pool=pool)

    values = asyncio.run(repository.ledger_stats())

    assert values == {
        "totalTrades": 7,
        "totalAmountAtomic": "49000000",
        "agentCount": 4,
    }
    assert "JOIN arena402.settlement_confirmations" in pool.fetchrow_query


def test_ledger_migration_adds_receipt_metadata_and_cross_game_indexes() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "ADD COLUMN facilitator_address TEXT" in sql
    assert "settlement_confirmations_facilitator_address_check" in sql
    assert "settlement_intents_ledger_created_idx" in sql
    assert "settlement_intents_ledger_game_idx" in sql
    assert "settlement_intents_ledger_buyer_agent_idx" in sql
    assert "settlement_intents_ledger_seller_agent_idx" in sql
    assert "settlement_intents_ledger_good_idx" in sql


def test_api_deployment_receives_backend_owned_chain_metadata() -> None:
    local_compose = (ROOT / "docker-compose.local.yml").read_text(encoding="utf-8")
    production_compose = (ROOT / "docker-compose.production.yml").read_text(
        encoding="utf-8"
    )
    generator = (ROOT / "deploy" / "scripts" / "generate-env.sh").read_text(
        encoding="utf-8"
    )

    for value in (local_compose, production_compose):
        assert "ADX_ARENA_EXPLORER_TX_URL_TEMPLATE:" in value
        assert "ADX_CURRENT_GAME_CHAIN_ID:" in value
    assert (
        "ADX_ARENA_EXPLORER_TX_URL_TEMPLATE="
        "https://testnet.blockscout.injective.network/tx/{txHash}"
    ) in generator
