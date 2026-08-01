from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from db.schema_identity import (
    SchemaIdentityError,
    expected_migration_manifest,
    verify_repository_schema_identity,
    verify_schema_identity,
)
from deploy.scripts.migrate import migration_files
from web.metrics import postgres_readiness


class _Pool:
    def __init__(
        self,
        applied: dict[str, str],
        *,
        available: bool = True,
    ) -> None:
        self.applied = dict(applied)
        self.available = available
        self.fetch_calls = 0

    async def fetch(self, _query: str) -> list[dict[str, str]]:
        self.fetch_calls += 1
        return [
            {"migration_name": name, "sha256": checksum}
            for name, checksum in sorted(self.applied.items())
        ]

    async def fetchval(self, _query: str) -> int:
        if not self.available:
            raise RuntimeError("database unavailable")
        return 1


class _Repository:
    def __init__(self, pool: _Pool | None) -> None:
        self._pool = pool


def _write_migration(root: Path, name: str, body: str) -> str:
    path = root / name
    path.write_bytes(body.encode("utf-8"))
    normalized = path.read_text(encoding="utf-8")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def test_manifest_matches_runner_selection_and_normalizes_newlines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector_checksum = _write_migration(
        tmp_path,
        "002_connector_gateway.sql",
        "BEGIN;\r\nSELECT 1;\r\nCOMMIT;\r\n",
    )
    arena_checksum = _write_migration(
        tmp_path,
        "003_arena_agent_runtime.sql",
        "BEGIN;\nSELECT 2;\nCOMMIT;\n",
    )
    _write_migration(
        tmp_path,
        "001_initial_schema.sql",
        "SELECT 'not selected';\n",
    )

    manifest = expected_migration_manifest(tmp_path)
    assert manifest == {
        "002_connector_gateway.sql": connector_checksum,
        "003_arena_agent_runtime.sql": arena_checksum,
    }
    monkeypatch.setenv("ADX_CONNECTOR_MIGRATIONS_DIR", str(tmp_path))
    runner_names = [path.name for path in migration_files("all")]
    assert list(manifest) == runner_names


def test_exact_schema_identity_is_accepted() -> None:
    expected = {
        "002_connector_gateway.sql": "connector-sha",
        "003_arena_agent_runtime.sql": "arena-sha",
    }
    pool = _Pool(expected)

    asyncio.run(verify_schema_identity(pool, expected=expected))

    assert pool.fetch_calls == 1


@pytest.mark.parametrize(
    ("applied", "reason"),
    [
        (
            {"002_connector_gateway.sql": "connector-sha"},
            "missing=003_arena_agent_runtime.sql",
        ),
        (
            {
                "002_connector_gateway.sql": "connector-sha",
                "003_arena_agent_runtime.sql": "arena-sha",
                "011_arena_scalable_games.sql": "orphan-sha",
            },
            "unexpected=011_arena_scalable_games.sql",
        ),
        (
            {
                "002_connector_gateway.sql": "connector-sha",
                "003_arena_agent_runtime.sql": "changed",
            },
            "checksum_mismatch=003_arena_agent_runtime.sql",
        ),
    ],
)
def test_schema_identity_fails_closed_on_manifest_drift(
    applied: dict[str, str],
    reason: str,
) -> None:
    expected = {
        "002_connector_gateway.sql": "connector-sha",
        "003_arena_agent_runtime.sql": "arena-sha",
    }

    with pytest.raises(SchemaIdentityError, match=reason):
        asyncio.run(verify_schema_identity(_Pool(applied), expected=expected))


def test_repository_schema_identity_requires_an_initialized_pool() -> None:
    with pytest.raises(
        SchemaIdentityError,
        match="schema_identity_pool_unavailable",
    ):
        asyncio.run(
            verify_repository_schema_identity(
                {"arena": _Repository(None)}
            )
        )


def test_postgres_readiness_includes_schema_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool({"002_connector_gateway.sql": "connector-sha"})

    async def verify(_pool: _Pool) -> None:
        assert _pool is pool

    monkeypatch.setattr("web.metrics.verify_schema_identity", verify)

    assert asyncio.run(
        postgres_readiness({"arena": _Repository(pool)})
    ) == {
        "arena": "ok",
        "schema_identity": "ok",
    }


def test_postgres_readiness_fails_when_schema_identity_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool({"002_connector_gateway.sql": "connector-sha"})

    async def reject(_pool: _Pool) -> None:
        raise SchemaIdentityError("schema_identity_mismatch")

    monkeypatch.setattr("web.metrics.verify_schema_identity", reject)

    assert asyncio.run(
        postgres_readiness({"arena": _Repository(pool)})
    ) == {
        "arena": "ok",
        "schema_identity": "unavailable",
    }
