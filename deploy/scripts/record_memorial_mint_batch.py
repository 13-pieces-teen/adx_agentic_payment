"""Record a Blockscout-confirmed public mint manifest in PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path

import asyncpg


HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")


def load_confirmed_manifest(path: Path) -> dict[str, object]:
    if not path.is_absolute() or not path.is_file():
        raise RuntimeError("manifest must be an absolute regular file")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("campaign") != "arena402-genesis":
        raise RuntimeError("unsupported memorial campaign")
    records = manifest.get("records")
    if not isinstance(records, list) or not records:
        raise RuntimeError("manifest contains no records")
    expected = int(records[0]["tokenId"])
    for record in records:
        if (
            int(record.get("tokenId", -1)) != expected
            or record.get("status") != "confirmed"
            or not HASH.fullmatch(str(record.get("txHash", "")))
            or int(record.get("blockNumber", -1)) < 0
        ):
            raise RuntimeError("manifest confirmation is invalid")
        expected += 1
    return manifest


async def record_confirmed_batch(
    manifest: dict[str, object],
    *,
    database_url: str,
) -> int:
    records = manifest["records"]
    assert isinstance(records, list)
    digest = str(manifest["addressDigest"])
    transaction_hashes = {str(record["txHash"]).lower() for record in records}
    if len(transaction_hashes) != 1:
        raise RuntimeError("one batch must have exactly one transaction hash")
    transaction_hash = transaction_hashes.pop()
    block_numbers = {int(record["blockNumber"]) for record in records}
    if len(block_numbers) != 1:
        raise RuntimeError("one batch must have exactly one block number")
    block_number = block_numbers.pop()

    connection = await asyncpg.connect(database_url, command_timeout=30)
    try:
        async with connection.transaction():
            await connection.execute("SET LOCAL ROLE adx_arena_migration")
            batch = await connection.fetchrow(
                """
                SELECT first_token_id, last_token_id, address_digest, status
                FROM arena402.memorial_mint_batches
                WHERE batch_id = $1 AND campaign_id = $2
                FOR UPDATE
                """,
                str(manifest["batchId"]),
                "arena402-genesis",
            )
            if batch is None:
                raise RuntimeError("prepared memorial batch not found")
            if (
                int(batch["first_token_id"]) != int(records[0]["tokenId"])
                or int(batch["last_token_id"]) != int(records[-1]["tokenId"])
                or str(batch["address_digest"]) != digest
            ):
                raise RuntimeError("confirmed manifest does not match prepared batch")
            if str(batch["status"]) == "confirmed":
                return 0

            for record in records:
                updated = await connection.execute(
                    """
                    UPDATE arena402.memorial_awards
                    SET mint_status = 'minted',
                        mint_tx_hash = $3,
                        mint_block_number = $4,
                        submitted_at = COALESCE(submitted_at, clock_timestamp()),
                        minted_at = clock_timestamp(),
                        last_error = NULL
                    WHERE campaign_id = $1
                      AND token_id = $2
                      AND wallet_id = $5
                      AND wallet_address = $6
                      AND mint_status = 'reserved'
                    """,
                    "arena402-genesis",
                    int(record["tokenId"]),
                    transaction_hash,
                    block_number,
                    str(record["walletId"]),
                    str(record["address"]).lower(),
                )
                if updated != "UPDATE 1":
                    raise RuntimeError(
                        f"memorial award conflict at token {record['tokenId']}"
                    )
                await connection.execute(
                    """
                    UPDATE arena402.memorial_wallet_inventory
                    SET status = 'minted',
                        updated_at = clock_timestamp()
                    WHERE campaign_id = $1 AND token_id = $2
                    """,
                    "arena402-genesis",
                    int(record["tokenId"]),
                )

            await connection.execute(
                """
                UPDATE arena402.memorial_mint_batches
                SET status = 'confirmed',
                    tx_hash = $2,
                    block_number = $3,
                    submitted_at = COALESCE(submitted_at, clock_timestamp()),
                    confirmed_at = clock_timestamp(),
                    updated_at = clock_timestamp(),
                    last_error = NULL
                WHERE batch_id = $1
                """,
                str(manifest["batchId"]),
                transaction_hash,
                block_number,
            )
            return len(records)
    finally:
        await connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist confirmation; default only validates the manifest.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    confirmed = load_confirmed_manifest(args.manifest.resolve())
    records = confirmed["records"]
    assert isinstance(records, list)
    if args.apply:
        dsn = (
            os.getenv("ADX_DATABASE_ADMIN_URL")
            or os.getenv("DATABASE_URL")
            or ""
        ).strip()
        if not dsn:
            raise SystemExit(
                "ADX_DATABASE_ADMIN_URL or DATABASE_URL is required"
            )
        changed = asyncio.run(
            record_confirmed_batch(confirmed, database_url=dsn)
        )
        print(f"memorial mint confirmations recorded: {changed}")
    else:
        print(
            f"memorial mint manifest validated: {len(records)}; "
            f"batch: {confirmed['batchId']}"
        )
