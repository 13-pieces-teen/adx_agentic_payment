"""Apply approved PostgreSQL migration scopes once and in deterministic order."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import Literal, cast

import asyncpg


MigrationScope = Literal["connector", "arena", "all"]

MIGRATION_PATTERNS: dict[str, re.Pattern[str]] = {
    "connector": re.compile(
        r"^\d+_connector(?:_gateway)?(?:_[a-z0-9_]+)?\.sql$"
    ),
    "arena": re.compile(
        r"^\d+_(?:arena|hosted_agent)(?:_[a-z0-9_]+)+\.sql$"
    ),
}
LOCK_NAME = "adx_schema_migrations"
DEFAULT_SCOPE: MigrationScope = "connector"


def _migration_sort_key(path: Path) -> tuple[int, str]:
    sequence, _, _ = path.name.partition("_")
    return int(sequence), path.name


def migration_files(scope: MigrationScope = DEFAULT_SCOPE) -> list[Path]:
    if scope not in ("connector", "arena", "all"):
        raise ValueError(f"Unsupported migration scope: {scope}")
    root = Path(os.getenv("ADX_CONNECTOR_MIGRATIONS_DIR", "/app/db/migrations"))
    if not root.is_dir():
        raise RuntimeError(f"Migration directory does not exist: {root}")

    selected_patterns = (
        tuple(MIGRATION_PATTERNS.values())
        if scope == "all"
        else (MIGRATION_PATTERNS[scope],)
    )
    files = sorted(
        (
            path
            for path in root.glob("*.sql")
            if any(pattern.fullmatch(path.name) for pattern in selected_patterns)
        ),
        key=_migration_sort_key,
    )
    if not files:
        raise RuntimeError(f"No {scope} migrations found in {root}")
    return files


async def connect_with_retry(dsn: str) -> asyncpg.Connection:
    delay = 1.0
    for attempt in range(1, 31):
        try:
            return await asyncpg.connect(dsn, command_timeout=120)
        except (OSError, asyncpg.PostgresError):
            if attempt == 30:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 1.4, 5.0)
    raise AssertionError("unreachable")


async def migrate(scope: MigrationScope = DEFAULT_SCOPE) -> None:
    dsn = os.getenv("ADX_CONNECTOR_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("ADX_CONNECTOR_DATABASE_URL or DATABASE_URL is required")

    connection = await connect_with_retry(dsn)
    try:
        await connection.execute("SELECT pg_advisory_lock(hashtext($1))", LOCK_NAME)
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS adx_schema_migrations (
                migration_name text PRIMARY KEY,
                sha256 text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )

        prepared: list[tuple[Path, str, str]] = []
        for path in migration_files(scope):
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            # SQL files remain independently runnable with psql and may include
            # BEGIN/COMMIT. This runner owns the atomic transaction, so strip
            # only one matching outer pair before execution.
            executable_sql = re.sub(r"^\s*BEGIN;\s*", "", sql, count=1)
            executable_sql = re.sub(r"\s*COMMIT;\s*$", "", executable_sql, count=1)
            prepared.append((path, checksum, executable_sql))

        applied_rows = await connection.fetch(
            """
            SELECT migration_name, sha256
            FROM adx_schema_migrations
            ORDER BY migration_name
            """
        )
        applied = {
            str(row["migration_name"]): str(row["sha256"])
            for row in applied_rows
        }
        expected = {
            path.name: checksum
            for path, checksum, _ in prepared
        }
        selected_patterns = (
            tuple(MIGRATION_PATTERNS.values())
            if scope == "all"
            else (MIGRATION_PATTERNS[scope],)
        )
        unexpected = sorted(
            name
            for name in applied.keys() - expected.keys()
            if scope == "all"
            or any(
                pattern.fullmatch(name)
                for pattern in selected_patterns
            )
        )
        changed = sorted(
            name
            for name in applied.keys() & expected.keys()
            if applied[name] != expected[name]
        )
        identity_errors: list[str] = []
        if unexpected:
            identity_errors.append(
                "Applied migration missing from disk: "
                + ", ".join(unexpected)
            )
        if changed:
            identity_errors.append(
                "Applied migration changed on disk: "
                + ", ".join(changed)
            )
        if identity_errors:
            raise RuntimeError("; ".join(identity_errors))

        for path, checksum, executable_sql in prepared:
            if path.name in applied:
                print(f"migration already applied: {path.name}", flush=True)
                continue

            async with connection.transaction():
                await connection.execute(executable_sql)
                # A migration may use SET LOCAL ROLE to constrain its DDL.
                # Restore the authenticated migration runner before recording
                # the checksum in the runner-owned metadata table.
                await connection.execute("RESET ROLE")
                await connection.execute(
                    """
                    INSERT INTO adx_schema_migrations (migration_name, sha256)
                    VALUES ($1, $2)
                    """,
                    path.name,
                    checksum,
                )
            print(f"migration applied: {path.name}", flush=True)
    finally:
        try:
            await connection.execute(
                "SELECT pg_advisory_unlock(hashtext($1))", LOCK_NAME
            )
        finally:
            await connection.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply approved Arena 402 PostgreSQL migrations."
    )
    parser.add_argument(
        "--scope",
        choices=("connector", "arena", "all"),
        default=os.getenv("ADX_MIGRATION_SCOPE", DEFAULT_SCOPE),
        help=(
            "Migration family to apply. Defaults to connector for backward "
            "compatibility; production composition should use all."
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    arguments = parse_args()
    asyncio.run(migrate(cast(MigrationScope, arguments.scope)))
