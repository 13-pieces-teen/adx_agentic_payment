"""Fail-closed runtime verification for the applied migration identity."""

from __future__ import annotations

import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


_MIGRATION_PATTERNS = (
    re.compile(r"^\d+_connector(?:_gateway)?(?:_[a-z0-9_]+)?\.sql$"),
    re.compile(r"^\d+_(?:arena|hosted_agent)(?:_[a-z0-9_]+)+\.sql$"),
)


class SchemaIdentityError(RuntimeError):
    """Raised when a database does not exactly match the packaged schema."""


def _migration_sort_key(path: Path) -> tuple[int, str]:
    sequence, _, _ = path.name.partition("_")
    return int(sequence), path.name


def _migration_root(directory: str | Path | None = None) -> Path:
    if directory is not None:
        return Path(directory)
    configured = os.getenv("ADX_CONNECTOR_MIGRATIONS_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent / "migrations"


@lru_cache(maxsize=8)
def _manifest_for_root(root_text: str) -> tuple[tuple[str, str], ...]:
    root = Path(root_text)
    if not root.is_dir():
        raise SchemaIdentityError(
            f"schema_identity_migration_directory_missing:{root}"
        )
    paths = sorted(
        (
            path
            for path in root.glob("*.sql")
            if any(
                pattern.fullmatch(path.name)
                for pattern in _MIGRATION_PATTERNS
            )
        ),
        key=_migration_sort_key,
    )
    if not paths:
        raise SchemaIdentityError(
            f"schema_identity_migration_manifest_empty:{root}"
        )
    return tuple(
        (
            path.name,
            hashlib.sha256(
                path.read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest(),
        )
        for path in paths
    )


def expected_migration_manifest(
    directory: str | Path | None = None,
) -> dict[str, str]:
    """Return the exact ordered migration manifest packaged with this process."""

    root = _migration_root(directory).resolve()
    return dict(_manifest_for_root(str(root)))


async def verify_schema_identity(
    pool: Any,
    *,
    expected: Mapping[str, str] | None = None,
) -> None:
    """Require exact migration names and checksums before serving or working."""

    expected_manifest = dict(
        expected_migration_manifest()
        if expected is None
        else expected
    )
    rows = await pool.fetch(
        """
        SELECT migration_name, sha256
        FROM public.adx_schema_migrations
        ORDER BY migration_name
        """
    )
    applied = {
        str(row["migration_name"]): str(row["sha256"])
        for row in rows
    }

    missing = sorted(expected_manifest.keys() - applied.keys())
    unexpected = sorted(applied.keys() - expected_manifest.keys())
    changed = sorted(
        name
        for name in expected_manifest.keys() & applied.keys()
        if expected_manifest[name] != applied[name]
    )
    if missing or unexpected or changed:
        details: list[str] = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if unexpected:
            details.append(f"unexpected={','.join(unexpected)}")
        if changed:
            details.append(f"checksum_mismatch={','.join(changed)}")
        raise SchemaIdentityError(
            "schema_identity_mismatch:" + ";".join(details)
        )


async def verify_repository_schema_identity(
    repositories: Mapping[str, object],
) -> None:
    """Verify the shared database behind a configured repository collection."""

    for repository in repositories.values():
        pool = getattr(repository, "_pool", None)
        if pool is not None:
            await verify_schema_identity(pool)
            return
    if repositories:
        raise SchemaIdentityError("schema_identity_pool_unavailable")


__all__ = [
    "SchemaIdentityError",
    "expected_migration_manifest",
    "verify_repository_schema_identity",
    "verify_schema_identity",
]
