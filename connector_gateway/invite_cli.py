"""Generate or persist a one-time high-entropy Connector invitation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

from .postgres_repository import PostgresConnectorRepository


def _new_invite() -> tuple[str, str]:
    invite = secrets.token_urlsafe(32)
    return invite, hashlib.sha256(invite.encode("utf-8")).hexdigest()


async def _persist_many(
    database_url: str,
    token_hashes: tuple[str, ...],
    expires_at: datetime,
) -> None:
    repository = PostgresConnectorRepository(database_url)
    try:
        await repository.initialize()
        for token_hash in token_hashes:
            await repository.seed_invite(token_hash, expires_at)
    finally:
        await repository.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a Connector invite and optionally persist its hash."
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help=(
            "Seed the invite in PostgreSQL using ADX_CONNECTOR_DATABASE_URL "
            "or DATABASE_URL from the process environment."
        ),
    )
    parser.add_argument(
        "--ttl-hours",
        type=int,
        default=24,
        help="Persisted invite lifetime in hours (default: 24; maximum: 168).",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Generate between 1 and 64 independent one-time invites.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print one machine-readable object without human labels.",
    )
    args = parser.parse_args()
    if not 1 <= args.ttl_hours <= 168:
        parser.error("--ttl-hours must be between 1 and 168")
    if not 1 <= args.count <= 64:
        parser.error("--count must be between 1 and 64")

    generated = tuple(_new_invite() for _ in range(args.count))
    invites = [invite for invite, _ in generated]
    token_hashes = tuple(token_hash for _, token_hash in generated)
    if args.persist:
        database_url = (
            os.getenv("ADX_CONNECTOR_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
        ).strip()
        if not database_url:
            parser.error(
                "--persist requires ADX_CONNECTOR_DATABASE_URL or DATABASE_URL"
            )
        expires_at = datetime.now(timezone.utc) + timedelta(hours=args.ttl_hours)
        asyncio.run(_persist_many(database_url, token_hashes, expires_at))
        if args.json:
            print(
                json.dumps(
                    {
                        "invites": invites,
                        "expiresAt": expires_at.isoformat(),
                    },
                    separators=(",", ":"),
                )
            )
        else:
            print("One-time Connector invites (shown once):")
            for invite in invites:
                print(invite)
            print(f"Expires at: {expires_at.isoformat()}")
    else:
        if args.json:
            print(
                json.dumps(
                    [
                        {"invite": invite, "tokenHash": token_hash}
                        for invite, token_hash in generated
                    ],
                    separators=(",", ":"),
                )
            )
        else:
            for invite, token_hash in generated:
                print("One-time Connector invite (shown once):")
                print(invite)
                print("ADX_BOOTSTRAP_INVITE_HASH:")
                print(token_hash)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
