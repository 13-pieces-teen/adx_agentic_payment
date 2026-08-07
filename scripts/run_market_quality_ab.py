"""Evaluate a paired Arena market-quality experiment manifest offline."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arena_game.market_experiment import (
    MarketExperimentError,
    evaluate_market_quality_experiment,
)


def _load_manifest(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MarketExperimentError(f"cannot read experiment manifest: {exc}") from None
    if not isinstance(value, dict):
        raise MarketExperimentError("experiment manifest must be a JSON object")
    return value


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    encoded = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(f"{encoded}\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise MarketExperimentError(f"cannot write experiment report: {exc}") from None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a privacy-safe paired Arena market-quality A/B report from "
            "frozen offline round observations."
        )
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        report = evaluate_market_quality_experiment(
            _load_manifest(args.input)
        )
        if args.output is not None:
            _write_report(args.output, report)
        else:
            print(
                json.dumps(
                    report,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
    except MarketExperimentError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
