"""Export two completed Arena Games into an offline A/B manifest."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arena_game.market_experiment import MarketExperimentError
from arena_game.market_experiment_export import (
    export_market_quality_manifest,
)


def _database_url(value: str | None) -> str:
    resolved = (
        value
        or os.getenv("ARENA_TEST_DATABASE_URL")
        or os.getenv("ADX_ARENA_CORE_DATABASE_URL")
        or ""
    ).strip()
    if not resolved:
        raise MarketExperimentError(
            "--database-url, ARENA_TEST_DATABASE_URL, or "
            "ADX_ARENA_CORE_DATABASE_URL is required"
        )
    return resolved


async def _export(args: argparse.Namespace) -> dict[str, object]:
    import asyncpg

    connection = await asyncpg.connect(
        _database_url(args.database_url),
        command_timeout=30,
    )
    try:
        async with connection.transaction(readonly=True):
            return await export_market_quality_manifest(
                connection,
                experiment_id=args.experiment_id,
                control_game_id=args.control_game_id,
                treatment_game_id=args.treatment_game_id,
            )
    finally:
        await connection.close()


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        raise MarketExperimentError(
            f"cannot write experiment manifest: {exc}"
        ) from None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Read two completed agent_a2a.v1 Games and export a paired "
            "market-quality A/B manifest."
        )
    )
    parser.add_argument("--database-url")
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--control-game-id", required=True)
    parser.add_argument("--treatment-game-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    try:
        manifest = asyncio.run(_export(args))
        _write_manifest(args.output, manifest)
    except MarketExperimentError as exc:
        parser.error(str(exc))
    except Exception:
        parser.error("database export failed")


if __name__ == "__main__":
    main()
