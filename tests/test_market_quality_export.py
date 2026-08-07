from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from arena_game.market_experiment import evaluate_market_quality_experiment
from arena_game.market_experiment_export import (
    _FUNNEL_SQL,
    export_market_quality_manifest,
)


class _Connection:
    async def fetchrow(
        self,
        query: str,
        game_id: str,
        round_id: str | None = None,
    ) -> dict[str, object] | None:
        if "FROM arena402.games" in query:
            return {
                "game_id": game_id,
                "phase": "completed",
                "event_seed": "shared-seed",
                "market_protocol": "agent_a2a.v1",
                "initial_net_worth_atomic": "20000000",
            }
        if "AS rfq_count" in query:
            is_treatment = game_id == "game-treatment"
            return {
                "rfq_count": 1 if is_treatment else 0,
                "engagement_count": 1 if is_treatment else 0,
                "deal_count": 1 if is_treatment else 0,
                "chain_confirmed_count": 1 if is_treatment else 0,
                "inventory_committed_count": 1 if is_treatment else 0,
                "invalid_action_count": 0,
                "default_action_count": 0 if is_treatment else 1,
                "invalid_structured_output_count": (
                    0 if is_treatment else 1
                ),
                "timeout_count": 0,
                "counter_offer_count": 1 if is_treatment else 0,
                "negotiation_action_count": 2 if is_treatment else 0,
                "counterparty_participant_count": 2 if is_treatment else 0,
            }
        raise AssertionError(f"unexpected fetchrow query: {query}")

    async def fetch(
        self,
        query: str,
        game_id: str,
    ) -> list[dict[str, object]]:
        if "FROM arena402.game_participants" in query:
            return [
                {
                    "agent_id": "agent-buyer",
                    "archetype": "aggressive",
                    "net_worth_atomic": (
                        20_500_000
                        if game_id == "game-treatment"
                        else 20_000_000
                    ),
                },
                {
                    "agent_id": "agent-seller",
                    "archetype": "conservative",
                    "net_worth_atomic": (
                        19_500_000
                        if game_id == "game-treatment"
                        else 20_000_000
                    ),
                },
            ]
        if "FROM arena402.rounds" in query:
            return [
                {
                    "round_id": f"round:{game_id}:1",
                    "round_index": 1,
                }
            ]
        if "FROM arena402.market_intents" in query:
            return [
                {
                    "round_id": f"round:{game_id}:1",
                    "participant_id": "agent-buyer",
                    "side": "buy",
                    "good_id": "grain",
                    "limit_price_atomic": (
                        2_100_000
                        if game_id == "game-treatment"
                        else 1_900_000
                    ),
                },
                {
                    "round_id": f"round:{game_id}:1",
                    "participant_id": "agent-seller",
                    "side": "sell",
                    "good_id": "grain",
                    "limit_price_atomic": 2_000_000,
                },
            ]
        raise AssertionError(f"unexpected fetch query: {query}")


def test_completed_games_export_directly_into_a_paired_report() -> None:
    manifest = asyncio.run(
        export_market_quality_manifest(
            _Connection(),
            experiment_id="exported-ab",
            control_game_id="game-control",
            treatment_game_id="game-treatment",
        )
    )

    report = evaluate_market_quality_experiment(manifest)

    assert manifest["control"]["sourceGameId"] == "game-control"
    assert manifest["treatment"]["sourceGameId"] == "game-treatment"
    assert report["status"] == "paired"
    assert report["deltas"]["priceCompatibleCapacity"] == 1
    assert report["deltas"]["inventoryCommittedCount"] == 1
    assert report["deltas"]["defaultActionCount"] == -1
    assert report["deltas"]["invalidStructuredOutputCount"] == -1


def test_timeout_funnel_distinguishes_defaults_from_real_timeouts() -> None:
    assert "AS default_action_count" in _FUNNEL_SQL
    assert "AS invalid_structured_output_count" in _FUNNEL_SQL
    assert "task.terminal_reason = 'deadline_exceeded'" in _FUNNEL_SQL
    timeout_clause = _FUNNEL_SQL.split("AS timeout_count", maxsplit=1)[0]
    timeout_clause = timeout_clause.rsplit("SELECT count(*)", maxsplit=1)[1]
    assert "task.status = 'defaulted'" not in timeout_clause


def test_export_cli_is_directly_executable() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/export_market_quality_ab.py",
            "--help",
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--control-game-id" in completed.stdout
    assert "--treatment-game-id" in completed.stdout
