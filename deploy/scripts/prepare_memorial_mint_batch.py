"""Prepare one public, contiguous mintBatch manifest from reserved awards."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg


CAMPAIGN_ID = "arena402-genesis"


async def prepare_batch(
    *,
    database_url: str,
    start_token_id: int,
    batch_size: int,
    output_path: Path,
    apply: bool,
) -> dict[str, object]:
    connection = await asyncpg.connect(database_url, command_timeout=30)
    try:
        campaign = await connection.fetchrow(
            """
            SELECT chain_id, contract_address
            FROM arena402.memorial_campaigns
            WHERE campaign_id = $1
            """,
            CAMPAIGN_ID,
        )
        if campaign is None:
            raise RuntimeError("memorial campaign not found")
        if campaign["contract_address"] is None:
            raise RuntimeError("memorial contract address is not configured")

        rows = await connection.fetch(
            """
            SELECT token_id, wallet_id, wallet_address
            FROM arena402.memorial_awards
            WHERE campaign_id = $1
              AND eligibility_status = 'reserved'
              AND mint_status = 'reserved'
              AND token_id >= $2
            ORDER BY token_id
            LIMIT $3
            """,
            CAMPAIGN_ID,
            start_token_id,
            batch_size,
        )
        if not rows:
            raise RuntimeError("no reserved memorial awards are ready")
        token_ids = [int(row["token_id"]) for row in rows]
        expected = list(range(start_token_id, start_token_id + len(rows)))
        if token_ids != expected:
            raise RuntimeError("reserved memorial awards are not contiguous")

        records = [
            {
                "tokenId": int(row["token_id"]),
                "walletId": str(row["wallet_id"]),
                "address": str(row["wallet_address"]),
                "status": "prepared",
            }
            for row in rows
        ]
        digest = _address_digest(records)
        manifest: dict[str, object] = {
            "version": 1,
            "campaign": CAMPAIGN_ID,
            "batchId": f"memorial-{start_token_id:03d}-{token_ids[-1]:03d}-{uuid4().hex[:12]}",
            "chainId": int(campaign["chain_id"]),
            "contractAddress": str(campaign["contract_address"]),
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "addressDigest": digest,
            "records": records,
        }
        if not apply:
            return manifest
        if not output_path.is_absolute():
            raise RuntimeError("--out must be an absolute path")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
            handle.write("\n")

        async with connection.transaction():
            await connection.execute("SET LOCAL ROLE adx_arena_migration")
            await connection.execute(
                """
                INSERT INTO arena402.memorial_mint_batches (
                    batch_id,
                    campaign_id,
                    first_token_id,
                    last_token_id,
                    address_digest
                )
                VALUES ($1, $2, $3, $4, $5)
                """,
                manifest["batchId"],
                CAMPAIGN_ID,
                start_token_id,
                token_ids[-1],
                digest,
            )
        return manifest
    finally:
        await connection.close()


def _address_digest(records: list[dict[str, object]]) -> str:
    addresses = [str(record["address"]).lower() for record in records]
    encoded = json.dumps(addresses, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-token-id", required=True, type=int)
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the manifest and persist its prepared batch record.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.start_token_id < 0 or args.start_token_id > 401:
        raise SystemExit("--start-token-id must be between 0 and 401")
    if args.batch_size < 1 or args.batch_size > 40:
        raise SystemExit("--batch-size must be between 1 and 40")
    dsn = (
        os.getenv("ADX_DATABASE_ADMIN_URL")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()
    if not dsn:
        raise SystemExit("ADX_DATABASE_ADMIN_URL or DATABASE_URL is required")
    result = asyncio.run(
        prepare_batch(
            database_url=dsn,
            start_token_id=args.start_token_id,
            batch_size=args.batch_size,
            output_path=args.out.resolve(),
            apply=args.apply,
        )
    )
    records = result["records"]
    assert isinstance(records, list)
    mode = "prepared" if args.apply else "previewed"
    print(
        f"memorial mint batch {mode}: {result['batchId']}; "
        f"tokens {records[0]['tokenId']}-{records[-1]['tokenId']}; "
        f"{result['addressDigest']}"
    )
