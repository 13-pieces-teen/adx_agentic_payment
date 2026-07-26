"""Mark one externally delivered memorial wallet credential as claimed."""

from __future__ import annotations

import argparse
import asyncio
import os
import re

import asyncpg


WALLET_ID = re.compile(r"^memorial-wallet-(?:0[0-3][0-9]{2}|040[01])$")


async def mark_claimed(*, database_url: str, wallet_id: str) -> bool:
    connection = await asyncpg.connect(database_url, command_timeout=30)
    try:
        async with connection.transaction():
            await connection.execute("SET LOCAL ROLE adx_arena_migration")
            result = await connection.execute(
                """
                UPDATE arena402.memorial_awards
                SET credential_status = 'claimed'
                WHERE campaign_id = 'arena402-genesis'
                  AND wallet_id = $1
                  AND credential_status = 'unclaimed'
                """,
                wallet_id,
            )
            if result == "UPDATE 1":
                return True
            exists = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM arena402.memorial_awards
                    WHERE campaign_id = 'arena402-genesis'
                      AND wallet_id = $1
                      AND credential_status = 'claimed'
                )
                """,
                wallet_id,
            )
            if not exists:
                raise RuntimeError("memorial wallet is not assigned")
            return False
    finally:
        await connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wallet-id", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the claim marker after the external handoff.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    wallet_id = str(args.wallet_id).strip()
    if not WALLET_ID.fullmatch(wallet_id):
        raise SystemExit("--wallet-id must be memorial-wallet-0000..0401")
    if not args.apply:
        print(f"memorial credential claim previewed: {wallet_id}")
        raise SystemExit(0)
    dsn = (
        os.getenv("ADX_DATABASE_ADMIN_URL")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()
    if not dsn:
        raise SystemExit("ADX_DATABASE_ADMIN_URL or DATABASE_URL is required")
    changed = asyncio.run(mark_claimed(database_url=dsn, wallet_id=wallet_id))
    state = "recorded" if changed else "already recorded"
    print(f"memorial credential claim {state}: {wallet_id}")
