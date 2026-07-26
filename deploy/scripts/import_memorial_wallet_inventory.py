"""Validate the external memorial CSV and import public fields only.

Dry-run is the default. The mnemonic, private key, and public key are required
for source-integrity checks but never leave this process or enter PostgreSQL.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import asyncpg


ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
CAMPAIGN_ID = "arena402-genesis"
EXPECTED_COUNT = 402
EXPECTED_CHAIN_ID = 1439
REQUIRED_FIELDS = frozenset(
    {
        "index",
        "token_id",
        "wallet_id",
        "ethereum_address",
        "public_key",
        "private_key",
        "mnemonic",
        "derivation_path",
        "chain_id",
        "network",
    }
)


@dataclass(frozen=True, slots=True)
class PublicMemorialWallet:
    token_id: int
    wallet_id: str
    account_address: str


def load_public_memorial_inventory(path: Path) -> tuple[
    list[PublicMemorialWallet], str
]:
    if not path.is_absolute() or not path.is_file():
        raise RuntimeError("memorial CSV must be an absolute regular file")

    checksum = hashlib.sha256()
    with path.open("rb") as raw:
        for chunk in iter(lambda: raw.read(1024 * 1024), b""):
            checksum.update(chunk)

    wallets: list[PublicMemorialWallet] = []
    addresses: set[str] = set()
    wallet_ids: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not REQUIRED_FIELDS <= set(reader.fieldnames or ()):
            raise RuntimeError("memorial CSV schema is unsupported")

        for expected_token_id, row in enumerate(reader):
            token_id = _integer(row, "token_id")
            index = _integer(row, "index")
            wallet_id = str(row.get("wallet_id", "")).strip()
            address = str(row.get("ethereum_address", "")).strip().lower()
            expected_wallet_id = (
                f"memorial-wallet-{expected_token_id:04d}"
            )

            if index != expected_token_id or token_id != expected_token_id:
                raise RuntimeError("memorial CSV token sequence is invalid")
            if wallet_id != expected_wallet_id:
                raise RuntimeError("memorial CSV wallet_id is invalid")
            if not ADDRESS.fullmatch(address):
                raise RuntimeError("memorial CSV address is invalid")
            if _integer(row, "chain_id") != EXPECTED_CHAIN_ID:
                raise RuntimeError("memorial CSV chain_id is invalid")
            if str(row.get("network", "")).strip() != "injective-evm-testnet":
                raise RuntimeError("memorial CSV network is invalid")
            if str(row.get("derivation_path", "")).strip() != "m/44'/60'/0'/0/0":
                raise RuntimeError("memorial CSV derivation path is invalid")
            if not all(
                str(row.get(field, "")).strip()
                for field in ("public_key", "private_key", "mnemonic")
            ):
                raise RuntimeError("memorial CSV secret material is incomplete")
            if wallet_id in wallet_ids or address in addresses:
                raise RuntimeError("memorial CSV contains a duplicate")

            wallet_ids.add(wallet_id)
            addresses.add(address)
            wallets.append(
                PublicMemorialWallet(
                    token_id=token_id,
                    wallet_id=wallet_id,
                    account_address=address,
                )
            )

    if len(wallets) != EXPECTED_COUNT:
        raise RuntimeError(
            f"memorial CSV must contain exactly {EXPECTED_COUNT} wallets"
        )
    return wallets, checksum.hexdigest()


async def import_memorial_inventory(
    wallets: list[PublicMemorialWallet],
    *,
    database_url: str,
    contract_address: str | None = None,
) -> int:
    connection = await asyncpg.connect(database_url, command_timeout=30)
    try:
        async with connection.transaction():
            await connection.execute("SET LOCAL ROLE adx_arena_migration")
            if contract_address is not None:
                await connection.execute(
                    """
                    UPDATE arena402.memorial_campaigns
                    SET contract_address = $2,
                        updated_at = clock_timestamp()
                    WHERE campaign_id = $1
                    """,
                    CAMPAIGN_ID,
                    contract_address,
                )
            for wallet in wallets:
                inserted = await connection.fetchval(
                    """
                    INSERT INTO arena402.memorial_wallet_inventory (
                        campaign_id,
                        token_id,
                        wallet_id,
                        account_address
                    )
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (campaign_id, token_id) DO NOTHING
                    RETURNING token_id
                    """,
                    CAMPAIGN_ID,
                    wallet.token_id,
                    wallet.wallet_id,
                    wallet.account_address,
                )
                if inserted is None:
                    matches = await connection.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM arena402.memorial_wallet_inventory
                            WHERE campaign_id = $1
                              AND token_id = $2
                              AND wallet_id = $3
                              AND account_address = $4
                        )
                        """,
                        CAMPAIGN_ID,
                        wallet.token_id,
                        wallet.wallet_id,
                        wallet.account_address,
                    )
                    if not matches:
                        raise RuntimeError(
                            "memorial wallet inventory conflict at token "
                            f"{wallet.token_id}"
                        )
            allocated = await connection.fetchval(
                "SELECT arena402.activate_memorial_campaign($1)",
                CAMPAIGN_ID,
            )
            return int(allocated or 0)
    finally:
        await connection.close()


def _integer(row: dict[str, str], field: str) -> int:
    raw = str(row.get(field, "")).strip()
    if not raw.isdigit():
        raise RuntimeError(f"memorial CSV {field} is invalid")
    return int(raw)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the external Founding 402 wallet CSV and optionally "
            "activate registration allocation."
        )
    )
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument(
        "--contract",
        help="Deployed memorial ERC-721 address to store with the campaign.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Import public fields and activate allocation; default is dry-run.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    inventory, source_checksum = load_public_memorial_inventory(
        args.csv.resolve()
    )
    if args.apply:
        contract = (
            str(args.contract).strip().lower()
            if args.contract is not None
            else None
        )
        if contract is not None and not ADDRESS.fullmatch(contract):
            raise SystemExit("--contract must be a valid EVM address")
        dsn = (
            os.getenv("ADX_DATABASE_ADMIN_URL")
            or os.getenv("DATABASE_URL")
            or ""
        ).strip()
        if not dsn:
            raise SystemExit(
                "ADX_DATABASE_ADMIN_URL or DATABASE_URL is required"
            )
        allocation_count = asyncio.run(
            import_memorial_inventory(
                inventory,
                database_url=dsn,
                contract_address=contract,
            )
        )
        print(
            f"memorial inventory imported: {len(inventory)}; "
            f"registrations allocated: {allocation_count}; "
            f"source sha256: {source_checksum}"
        )
    else:
        print(
            f"memorial inventory validated: {len(inventory)}; "
            f"source sha256: {source_checksum}"
        )
