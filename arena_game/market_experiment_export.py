"""Read-only export of completed Arena Games into a paired A/B manifest."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .market_experiment import (
    EXPERIMENT_SCHEMA_VERSION,
    MarketExperimentError,
)
from .portfolio import INITIAL_NET_WORTH_ATOMIC


_GAME_SQL = """
SELECT
    game_id,
    phase,
    event_seed,
    market_protocol,
    config_snapshot ->> 'initialNetWorthAtomic'
        AS initial_net_worth_atomic
FROM arena402.games
WHERE game_id = $1
"""

_PARTICIPANTS_SQL = """
SELECT
    participant.agent_id,
    COALESCE(strategy.archetype, 'custom') AS archetype,
    ranking.net_worth_atomic
FROM arena402.game_participants AS participant
JOIN arena402.rankings AS ranking
  ON ranking.game_id = participant.game_id
 AND ranking.game_participant_id = participant.game_participant_id
LEFT JOIN public.game_agents AS game_agent
  ON game_agent.game_agent_id = participant.game_participant_id
LEFT JOIN public.hosted_agent_strategy_revisions AS strategy
  ON strategy.strategy_revision_id =
     game_agent.hosted_strategy_revision_id
WHERE participant.game_id = $1
ORDER BY participant.agent_id
"""

_ROUNDS_SQL = """
SELECT round_id, round_index
FROM arena402.rounds
WHERE game_id = $1
ORDER BY round_index
"""

_INTENTS_SQL = """
SELECT
    intent.round_id,
    participant.agent_id AS participant_id,
    intent.side,
    intent.good_id,
    intent.limit_price_atomic
FROM arena402.market_intents AS intent
JOIN arena402.game_participants AS participant
  ON participant.game_participant_id = intent.game_participant_id
 AND participant.game_id = intent.game_id
WHERE intent.game_id = $1
ORDER BY intent.round_id, participant.agent_id
"""

_FUNNEL_SQL = """
SELECT
    (
        SELECT count(*)
        FROM arena402.market_negotiation_requests
        WHERE game_id = $1 AND round_id = $2
    ) AS rfq_count,
    (
        SELECT count(*)
        FROM arena402.market_engagements
        WHERE game_id = $1 AND round_id = $2
    ) AS engagement_count,
    (
        SELECT count(*)
        FROM arena402.market_deals
        WHERE game_id = $1 AND round_id = $2
    ) AS deal_count,
    (
        SELECT count(*)
        FROM arena402.settlement_intents
        WHERE game_id = $1
          AND round_id = $2
          AND status IN (
              'chain_confirmed_uncommitted',
              'inventory_committed'
          )
    ) AS chain_confirmed_count,
    (
        SELECT count(*)
        FROM arena402.settlement_intents
        WHERE game_id = $1
          AND round_id = $2
          AND status = 'inventory_committed'
    ) AS inventory_committed_count,
    (
        SELECT count(*)
        FROM public.arena_agent_task_results AS result
        JOIN public.arena_agent_tasks AS task
          ON task.task_id = result.task_id
        WHERE task.game_id = $1
          AND task.round_id = $2
          AND result.apply_status = 'rejected'
    ) AS invalid_action_count,
    (
        SELECT count(*)
        FROM public.arena_agent_tasks AS task
        WHERE task.game_id = $1
          AND task.round_id = $2
          AND task.status = 'defaulted'
    ) AS default_action_count,
    (
        SELECT count(*)
        FROM public.arena_agent_tasks AS task
        WHERE task.game_id = $1
          AND task.round_id = $2
          AND task.status = 'defaulted'
          AND task.terminal_reason = 'invalid_structured_output'
    ) AS invalid_structured_output_count,
    (
        SELECT count(*)
        FROM public.arena_agent_tasks AS task
        WHERE task.game_id = $1
          AND task.round_id = $2
          AND (
              task.terminal_reason = 'deadline_exceeded'
              OR EXISTS (
                  SELECT 1
                  FROM public.arena_agent_task_results AS result
                  WHERE result.task_id = task.task_id
                    AND result.runtime_status = 'timed_out'
              )
          )
    ) AS timeout_count,
    (
        SELECT count(*)
        FROM arena402.negotiation_messages
        WHERE game_id = $1
          AND round_id = $2
          AND action = 'propose'
    ) AS counter_offer_count,
    (
        SELECT count(*)
        FROM arena402.negotiation_messages
        WHERE game_id = $1 AND round_id = $2
    ) AS negotiation_action_count,
    (
        SELECT count(DISTINCT participant_id)
        FROM (
            SELECT buyer_participant_id AS participant_id
            FROM arena402.market_engagements
            WHERE game_id = $1 AND round_id = $2
            UNION
            SELECT seller_participant_id AS participant_id
            FROM arena402.market_engagements
            WHERE game_id = $1 AND round_id = $2
        ) AS counterparties
    ) AS counterparty_participant_count
"""


def _required_row(
    row: Mapping[str, object] | None,
    *,
    error: str,
) -> Mapping[str, object]:
    if row is None:
        raise MarketExperimentError(error)
    return row


async def _export_arm(
    connection: Any,
    *,
    arm_id: str,
    game_id: str,
    case_id: str,
) -> tuple[dict[str, object], dict[str, str]]:
    game = _required_row(
        await connection.fetchrow(_GAME_SQL, game_id),
        error="source Game was not found",
    )
    if str(game["phase"]) != "completed":
        raise MarketExperimentError("source Game must be completed")
    if str(game["market_protocol"]) != "agent_a2a.v1":
        raise MarketExperimentError(
            "source Game must use agent_a2a.v1"
        )
    if str(game["initial_net_worth_atomic"]) != str(
        INITIAL_NET_WORTH_ATOMIC
    ):
        raise MarketExperimentError(
            "source Game must use the frozen 20-gold initial net worth"
        )

    participant_rows = await connection.fetch(
        _PARTICIPANTS_SQL,
        game_id,
    )
    if not participant_rows:
        raise MarketExperimentError(
            "source Game must have terminal rankings"
        )
    participant_ids = [str(row["agent_id"]) for row in participant_rows]
    if len(set(participant_ids)) != len(participant_ids):
        raise MarketExperimentError(
            "source Game Agent identities must be unique"
        )
    archetypes = {
        str(row["agent_id"]): str(row["archetype"])
        for row in participant_rows
    }
    outcomes = [
        {
            "participantId": str(row["agent_id"]),
            "archetype": str(row["archetype"]),
            "netWorthAtomic": str(int(row["net_worth_atomic"])),
        }
        for row in participant_rows
    ]

    round_rows = await connection.fetch(_ROUNDS_SQL, game_id)
    if not round_rows:
        raise MarketExperimentError("source Game has no rounds")
    intents_by_round: dict[str, list[dict[str, object]]] = {
        str(row["round_id"]): [] for row in round_rows
    }
    for row in await connection.fetch(_INTENTS_SQL, game_id):
        round_id = str(row["round_id"])
        if round_id not in intents_by_round:
            raise MarketExperimentError(
                "source Game intent references an unknown round"
            )
        intents_by_round[round_id].append(
            {
                "participantId": str(row["participant_id"]),
                "side": str(row["side"]),
                "good": str(row["good_id"]),
                "limitPriceAtomic": str(
                    int(row["limit_price_atomic"])
                ),
            }
        )

    rounds: list[dict[str, object]] = []
    funnel_names = {
        "rfqCount": "rfq_count",
        "engagementCount": "engagement_count",
        "dealCount": "deal_count",
        "chainConfirmedCount": "chain_confirmed_count",
        "inventoryCommittedCount": "inventory_committed_count",
        "invalidActionCount": "invalid_action_count",
        "defaultActionCount": "default_action_count",
        "invalidStructuredOutputCount": (
            "invalid_structured_output_count"
        ),
        "timeoutCount": "timeout_count",
        "counterOfferCount": "counter_offer_count",
        "negotiationActionCount": "negotiation_action_count",
        "counterpartyParticipantCount": (
            "counterparty_participant_count"
        ),
    }
    for row in round_rows:
        round_id = str(row["round_id"])
        funnel_row = _required_row(
            await connection.fetchrow(
                _FUNNEL_SQL,
                game_id,
                round_id,
            ),
            error="source Game round funnel was not available",
        )
        rounds.append(
            {
                "roundIndex": int(row["round_index"]),
                "intents": intents_by_round[round_id],
                "funnel": {
                    public_name: int(funnel_row[database_name])
                    for public_name, database_name in funnel_names.items()
                },
            }
        )

    return (
        {
            "armId": arm_id,
            "sourceGameId": game_id,
            "cases": [
                {
                    "caseId": case_id,
                    "eventSeed": str(game["event_seed"]),
                    "participantIds": participant_ids,
                    "outcomes": outcomes,
                    "rounds": rounds,
                }
            ],
        },
        archetypes,
    )


async def export_market_quality_manifest(
    connection: Any,
    *,
    experiment_id: str,
    control_game_id: str,
    treatment_game_id: str,
) -> dict[str, object]:
    """Export two completed Games into one paired experiment manifest."""

    if not experiment_id:
        raise MarketExperimentError("experimentId is required")
    if (
        not control_game_id
        or not treatment_game_id
        or control_game_id == treatment_game_id
    ):
        raise MarketExperimentError(
            "control and treatment require different Game IDs"
        )
    case_id = f"{experiment_id}:pair-1"
    control, control_archetypes = await _export_arm(
        connection,
        arm_id="control",
        game_id=control_game_id,
        case_id=case_id,
    )
    treatment, treatment_archetypes = await _export_arm(
        connection,
        arm_id="treatment",
        game_id=treatment_game_id,
        case_id=case_id,
    )
    control_case = control["cases"][0]  # type: ignore[index]
    treatment_case = treatment["cases"][0]  # type: ignore[index]
    if control_case["eventSeed"] != treatment_case["eventSeed"]:
        raise MarketExperimentError(
            "paired source Games must use the same event seed"
        )
    if control_archetypes != treatment_archetypes:
        raise MarketExperimentError(
            "paired source Games must use the same Agent/archetype roster"
        )
    if len(control_case["rounds"]) != len(treatment_case["rounds"]):
        raise MarketExperimentError(
            "paired source Games must have the same round count"
        )
    return {
        "schemaVersion": EXPERIMENT_SCHEMA_VERSION,
        "experimentId": experiment_id,
        "control": control,
        "treatment": treatment,
    }


__all__ = ["export_market_quality_manifest"]
