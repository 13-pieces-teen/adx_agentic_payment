"""Import only public wallet metadata from the external testnet CSV.

The CSV is never copied into the repository or database. The command rejects
group/world-readable secret files and emits counts only.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
from pathlib import Path

import asyncpg


ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def load_public_inventory(path: Path, chain_id: int) -> list[tuple[str, int, str, str]]:
    if not path.is_absolute() or not path.is_file():
        raise RuntimeError("wallet CSV must be an absolute regular file")
    if path.stat().st_mode & 0o077:
        raise RuntimeError("wallet CSV must not be group/world accessible")
    values: list[tuple[str, int, str, str]] = []
    seen_addresses: set[str] = set()
    seen_wallet_ids: set[str] = set()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"index", "ethereum_address", "private_key"} <= set(
            reader.fieldnames or ()
        ):
            raise RuntimeError("wallet CSV schema is unsupported")
        for row in reader:
            index = str(row.get("index", "")).strip()
            address = str(row.get("ethereum_address", "")).strip().lower()
            # Require key presence, but never persist, return, or print it.
            has_key = bool(str(row.get("private_key", "")).strip())
            if not index.isdigit() or not ADDRESS.fullmatch(address) or not has_key:
                raise RuntimeError("wallet CSV contains an invalid row")
            wallet_id = f"agent-wallet-{index.zfill(4)}"
            if wallet_id in seen_wallet_ids or address in seen_addresses:
                raise RuntimeError("wallet CSV contains a duplicate")
            seen_wallet_ids.add(wallet_id)
            seen_addresses.add(address)
            values.append(
                (
                    wallet_id,
                    chain_id,
                    address,
                    f"agent-wallets.csv#{index}",
                )
            )
    if not values:
        raise RuntimeError("wallet CSV is empty")
    return values


async def import_inventory(
    values: list[tuple[str, int, str, str]],
    *,
    database_url: str,
) -> None:
    connection = await asyncpg.connect(database_url, command_timeout=30)
    try:
        async with connection.transaction():
            for wallet_id, chain_id, address, secret_ref in values:
                existing = await connection.fetchrow(
                    """
                    INSERT INTO arena402.wallet_inventory (
                        wallet_id, chain_id, account_address, secret_ref
                    )
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (wallet_id) DO NOTHING
                    RETURNING wallet_id
                    """,
                    wallet_id,
                    chain_id,
                    address,
                    secret_ref,
                )
                if existing is None:
                    matches = await connection.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM arena402.wallet_inventory
                            WHERE wallet_id = $1
                              AND chain_id = $2
                              AND account_address = $3
                              AND secret_ref = $4
                        )
                        """,
                        wallet_id,
                        chain_id,
                        address,
                        secret_ref,
                    )
                    if not matches:
                        raise RuntimeError("wallet inventory conflict")
    finally:
        await connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import public wallet inventory for Arena 402."
    )
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--chain-id", required=True, type=int)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to PostgreSQL; without this flag only validate and count.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.chain_id <= 0:
        raise SystemExit("--chain-id must be positive")
    inventory = load_public_inventory(args.csv, args.chain_id)
    if args.apply:
        dsn = (
            os.getenv("ADX_ARENA_CORE_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
        ).strip()
        if not dsn:
            raise SystemExit("ADX_ARENA_CORE_DATABASE_URL or DATABASE_URL is required")
        asyncio.run(import_inventory(inventory, database_url=dsn))
        print(f"wallet inventory imported: {len(inventory)}")
    else:
        print(f"wallet inventory validated: {len(inventory)}")
