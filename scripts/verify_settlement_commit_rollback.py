#!/usr/bin/env python3
"""Exercise the confirmed inventory commit inside a rolled-back transaction.

This is a development verifier, not a chain simulator. It temporarily inserts
synthetic submission/confirmation rows, checks the exactly-once inventory
transaction, and rolls the outer PostgreSQL transaction back unconditionally.
No synthetic chain claim remains in the database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arena_game import ChainConfirmation, PostgresPawnhouseRepository


class _Acquire:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def __aenter__(self) -> Any:
        return self._connection

    async def __aexit__(self, *_: object) -> None:
        return None


class _SingleConnectionPool:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self._connection)

    async def fetchrow(self, sql: str, *args: object) -> Any:
        return await self._connection.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: object) -> Any:
        return await self._connection.fetchval(sql, *args)

    async def fetch(self, sql: str, *args: object) -> Any:
        return await self._connection.fetch(sql, *args)


async def _main(args: argparse.Namespace) -> int:
    import asyncpg

    connection = await asyncpg.connect(args.database_url)
    outer = connection.transaction()
    await outer.start()
    rolled_back = False
    try:
        await connection.execute("SET LOCAL ROLE adx_arena_core")
        await connection.execute(
            "SET LOCAL search_path TO pg_catalog, arena402, public"
        )
        repository = PostgresPawnhouseRepository(
            "",
            pool=_SingleConnectionPool(connection),
        )
        game_id = await connection.fetchval(
            """
            SELECT game_id
            FROM arena402.settlement_intents
            WHERE settlement_intent_id = $1
            """,
            args.intent_id,
        )
        if game_id is None:
            raise RuntimeError("settlement intent not found")
        before_automation = await repository.automation_state(
            game_id=str(game_id)
        )
        if before_automation["action"] != "wait_settlement":
            raise RuntimeError(
                "game is not waiting for the selected settlement intent"
            )
        tx_hash = "0x" + "ab" * 32
        intent_hash = await connection.fetchval(
            """
            SELECT intent_hash
            FROM arena402.settlement_intents
            WHERE settlement_intent_id = $1
            """,
            args.intent_id,
        )
        if not isinstance(intent_hash, str):
            raise RuntimeError("settlement intent not found")
        authorization_nonce = (
            "0x" + intent_hash.removeprefix("sha256:")
        )
        await repository.record_settlement_approval(
            settlement_intent_id=args.intent_id,
            approved_intent_hash=intent_hash,
            authorization_nonce=authorization_nonce,
            approval_source="operator_cli",
        )
        await repository.record_settlement_submission(
            settlement_intent_id=args.intent_id,
            tx_hash=tx_hash,
            authorization_nonce=authorization_nonce,
            approved_intent_hash=intent_hash,
            submission_source="wallet",
        )
        intent, _ = await repository.settlement_confirmation_target(
            settlement_intent_id=args.intent_id
        )
        await repository.record_chain_confirmation(
            settlement_intent_id=args.intent_id,
            confirmation=ChainConfirmation(
                tx_hash=tx_hash,
                chain_id=intent.chain_id,
                facilitator_address="0x" + "44" * 20,
                token_address=intent.token_address,
                from_account=intent.buyer_account,
                to_account=intent.seller_account,
                amount_atomic=intent.amount_atomic,
                block_number=123,
                block_hash="0x" + "ef" * 32,
                confirmation_count=intent.required_confirmations,
                success=True,
            ),
        )
        committed = await repository.commit_confirmed_inventory(
            settlement_intent_id=args.intent_id
        )
        replay = await repository.commit_confirmed_inventory(
            settlement_intent_id=args.intent_id
        )
        if committed != replay:
            raise RuntimeError("inventory commit replay changed the receipt")
        after_automation = await repository.automation_state(
            game_id=str(game_id)
        )
        if after_automation["action"] != "advance_round":
            raise RuntimeError(
                "game did not become eligible for automatic advancement"
            )
        summary = {
            "settlementIntentId": args.intent_id,
            "gameId": str(game_id),
            "automationBefore": before_automation["action"],
            "automationAfter": after_automation["action"],
            "statusInsideTransaction": committed["status"],
            "buyerCashDeltaAtomic": str(
                int(committed["buyerCashAfterAtomic"])
                - int(committed["buyerCashBeforeAtomic"])
            ),
            "sellerCashDeltaAtomic": str(
                int(committed["sellerCashAfterAtomic"])
                - int(committed["sellerCashBeforeAtomic"])
            ),
            "buyerHoldingDelta": (
                committed["buyerHoldingAfter"]
                - committed["buyerHoldingBefore"]
            ),
            "sellerHoldingDelta": (
                committed["sellerHoldingAfter"]
                - committed["sellerHoldingBefore"]
            ),
            "idempotentReplay": True,
            "syntheticEvidencePersisted": False,
        }
    finally:
        await outer.rollback()
        rolled_back = True
        await connection.close()
    if not rolled_back:
        raise RuntimeError("verification transaction was not rolled back")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--intent-id", required=True)
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "ARENA_DATABASE_URL",
            (
                "postgresql://arena402_admin:"
                "arena402-local-admin-password@127.0.0.1:55432/arena402"
            ),
        ),
    )
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
