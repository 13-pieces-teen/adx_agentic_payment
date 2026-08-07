from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from arena_game.market_experiment import (
    MarketExperimentError,
    evaluate_market_quality_experiment,
)


def _arm(arm_id: str, *, buyer_limit: int) -> dict[str, object]:
    return {
        "armId": arm_id,
        "cases": [
            {
                "caseId": "case-seed-1",
                "eventSeed": "seed-1",
                "participantIds": ["buyer", "seller", "passer"],
                "outcomes": [
                    {
                        "participantId": "buyer",
                        "archetype": "aggressive",
                        "netWorthAtomic": "21000000",
                    },
                    {
                        "participantId": "seller",
                        "archetype": "conservative",
                        "netWorthAtomic": "19000000",
                    },
                    {
                        "participantId": "passer",
                        "archetype": "balanced",
                        "netWorthAtomic": "20000000",
                    },
                ],
                "rounds": [
                    {
                        "roundIndex": 1,
                        "intents": [
                            {
                                "participantId": "buyer",
                                "side": "buy",
                                "good": "grain",
                                "limitPriceAtomic": str(buyer_limit),
                            },
                            {
                                "participantId": "seller",
                                "side": "sell",
                                "good": "grain",
                                "limitPriceAtomic": "2000000",
                            },
                        ],
                        "funnel": {
                            "rfqCount": 1,
                            "engagementCount": 1,
                            "dealCount": 1,
                            "chainConfirmedCount": 1,
                            "inventoryCommittedCount": 1,
                            "invalidActionCount": 0,
                            "defaultActionCount": 0,
                            "invalidStructuredOutputCount": 0,
                            "timeoutCount": 0,
                            "counterOfferCount": 1,
                            "negotiationActionCount": 2,
                            "counterpartyParticipantCount": 2,
                        },
                    }
                ],
            }
        ],
    }


def test_experiment_reports_paired_market_quality_without_private_limits() -> None:
    report = evaluate_market_quality_experiment(
        {
            "schemaVersion": "arena.market-quality-experiment.v1",
            "experimentId": "market-ab-1",
            "control": _arm("baseline", buyer_limit=1_900_000),
            "treatment": _arm("candidate", buyer_limit=2_100_000),
        }
    )

    assert report["schemaVersion"] == "arena.market-quality-ab-report.v1"
    assert report["status"] == "paired"
    assert report["control"]["totals"] == {
        "participantRoundCount": 3,
        "intentCount": 2,
        "passCount": 1,
        "buyIntentCount": 1,
        "sellIntentCount": 1,
        "oppositeSideCapacity": 1,
        "priceCompatibleCapacity": 0,
        "rfqCount": 1,
        "engagementCount": 1,
        "dealCount": 1,
        "chainConfirmedCount": 1,
        "inventoryCommittedCount": 1,
        "invalidActionCount": 0,
        "defaultActionCount": 0,
        "invalidStructuredOutputCount": 0,
        "timeoutCount": 0,
        "counterOfferCount": 1,
        "negotiationActionCount": 2,
        "counterpartyParticipantCount": 2,
    }
    assert report["treatment"]["totals"]["priceCompatibleCapacity"] == 1
    assert report["deltas"]["priceCompatibleCapacity"] == 1
    assert report["deltas"]["compatibleRoundRate"] == 1.0
    assert report["control"]["outcomes"] == {
        "participantCount": 3,
        "meanNetWorthAtomicFloor": "20000000",
        "minimumNetWorthAtomic": "19000000",
        "meanReturnBps": 0,
        "downsideReturnBps": -500,
        "byArchetype": {
            "aggressive": {
                "participantCount": 1,
                "meanReturnBps": 500,
                "downsideReturnBps": 500,
            },
            "balanced": {
                "participantCount": 1,
                "meanReturnBps": 0,
                "downsideReturnBps": 0,
            },
            "conservative": {
                "participantCount": 1,
                "meanReturnBps": -500,
                "downsideReturnBps": -500,
            },
        },
    }
    assert "buyer" not in str(report)
    assert "limitPriceAtomic" not in str(report)


def test_cli_writes_a_durable_paired_report(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    report_path = tmp_path / "report.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schemaVersion": "arena.market-quality-experiment.v1",
                "experimentId": "market-ab-cli",
                "control": _arm("baseline", buyer_limit=1_900_000),
                "treatment": _arm("candidate", buyer_limit=2_100_000),
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_market_quality_ab.py",
            "--input",
            str(manifest_path),
            "--output",
            str(report_path),
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "paired"
    assert report["experimentId"] == "market-ab-cli"


def test_experiment_rejects_unpaired_event_or_participant_design() -> None:
    treatment = _arm("candidate", buyer_limit=2_100_000)
    treatment["cases"][0]["eventSeed"] = "different-seed"  # type: ignore[index]

    with pytest.raises(MarketExperimentError, match="same paired design"):
        evaluate_market_quality_experiment(
            {
                "schemaVersion": "arena.market-quality-experiment.v1",
                "experimentId": "market-ab-unpaired",
                "control": _arm("baseline", buyer_limit=1_900_000),
                "treatment": treatment,
            }
        )


def test_experiment_accepts_zero_terminal_net_worth() -> None:
    control = _arm("baseline", buyer_limit=1_900_000)
    treatment = _arm("candidate", buyer_limit=2_100_000)
    control["cases"][0]["outcomes"][0]["netWorthAtomic"] = "0"  # type: ignore[index]
    treatment["cases"][0]["outcomes"][0]["netWorthAtomic"] = "0"  # type: ignore[index]

    report = evaluate_market_quality_experiment(
        {
            "schemaVersion": "arena.market-quality-experiment.v1",
            "experimentId": "market-ab-zero-net-worth",
            "control": control,
            "treatment": treatment,
        }
    )

    assert report["control"]["outcomes"]["minimumNetWorthAtomic"] == "0"
    assert report["control"]["outcomes"]["downsideReturnBps"] == -10_000
