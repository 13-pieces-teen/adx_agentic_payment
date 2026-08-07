"""Deterministic, privacy-safe offline market-quality A/B evaluation."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from .goods import GOOD_IDS, require_good
from .liquidity import LiquidityIntent, summarize_round_liquidity
from .portfolio import INITIAL_NET_WORTH_ATOMIC


EXPERIMENT_SCHEMA_VERSION = "arena.market-quality-experiment.v1"
REPORT_SCHEMA_VERSION = "arena.market-quality-ab-report.v1"

_FUNNEL_FIELDS = (
    "rfqCount",
    "engagementCount",
    "dealCount",
    "chainConfirmedCount",
    "inventoryCommittedCount",
    "invalidActionCount",
    "defaultActionCount",
    "invalidStructuredOutputCount",
    "timeoutCount",
    "counterOfferCount",
    "negotiationActionCount",
    "counterpartyParticipantCount",
)
_TOTAL_FIELDS = (
    "participantRoundCount",
    "intentCount",
    "passCount",
    "buyIntentCount",
    "sellIntentCount",
    "oppositeSideCapacity",
    "priceCompatibleCapacity",
    *_FUNNEL_FIELDS,
)
_ARCHETYPES = {"aggressive", "balanced", "conservative", "custom"}


class MarketExperimentError(ValueError):
    """A safe validation failure for an offline experiment manifest."""


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MarketExperimentError(f"{field} must be an object")
    return value


def _array(value: object, field: str) -> Sequence[object]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise MarketExperimentError(f"{field} must be an array")
    return value


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise MarketExperimentError(
            f"{field} must be a non-empty bounded identifier"
        )
    return value


def _non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MarketExperimentError(
            f"{field} must be a non-negative integer"
        )
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise MarketExperimentError(f"{field} must be a positive integer")
    try:
        resolved = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise MarketExperimentError(
            f"{field} must be a positive integer"
        ) from None
    if resolved <= 0 or str(resolved) != str(value):
        raise MarketExperimentError(f"{field} must be a positive integer")
    return resolved


def _non_negative_atomic(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise MarketExperimentError(
            f"{field} must be a non-negative atomic integer"
        )
    try:
        resolved = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise MarketExperimentError(
            f"{field} must be a non-negative atomic integer"
        ) from None
    if resolved < 0 or str(resolved) != str(value):
        raise MarketExperimentError(
            f"{field} must be a non-negative atomic integer"
        )
    return resolved


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def _entropy(counts: Sequence[int]) -> float:
    total = sum(counts)
    if total == 0:
        return 0.0
    value = -sum(
        (count / total) * math.log2(count / total)
        for count in counts
        if count
    )
    return round(value, 6)


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _arm_report(arm: Mapping[str, object]) -> tuple[dict[str, object], object]:
    arm_id = _identifier(arm.get("armId"), "armId")
    raw_cases = _array(arm.get("cases"), "cases")
    if not raw_cases:
        raise MarketExperimentError("an experiment arm requires at least one case")

    totals = {field: 0 for field in _TOTAL_FIELDS}
    by_good = {
        good: {
            "buyIntentCount": 0,
            "sellIntentCount": 0,
            "oppositeSideCapacity": 0,
            "priceCompatibleCapacity": 0,
        }
        for good in GOOD_IDS
    }
    round_count = 0
    compatible_round_count = 0
    opposite_round_count = 0
    private_fingerprint_cases: list[object] = []
    design_cases: list[object] = []
    seen_case_ids: set[str] = set()
    outcome_values: list[int] = []
    outcome_returns: list[int] = []
    outcomes_by_archetype: dict[str, list[int]] = {}

    for raw_case in sorted(
        raw_cases,
        key=lambda item: _identifier(
            _object(item, "case").get("caseId"),
            "caseId",
        ),
    ):
        case = _object(raw_case, "case")
        case_id = _identifier(case.get("caseId"), "caseId")
        if case_id in seen_case_ids:
            raise MarketExperimentError("caseId must be unique within an arm")
        seen_case_ids.add(case_id)
        event_seed = _identifier(case.get("eventSeed"), "eventSeed")
        participant_ids = tuple(
            _identifier(value, "participantId")
            for value in _array(case.get("participantIds"), "participantIds")
        )
        if not participant_ids or len(set(participant_ids)) != len(
            participant_ids
        ):
            raise MarketExperimentError(
                "participantIds must be non-empty and unique"
            )
        participant_set = set(participant_ids)
        raw_outcomes = _array(case.get("outcomes"), "outcomes")
        if len(raw_outcomes) != len(participant_ids):
            raise MarketExperimentError(
                "outcomes must contain every frozen participant exactly once"
            )
        case_outcomes: list[object] = []
        case_archetypes: dict[str, str] = {}
        for raw_outcome in raw_outcomes:
            outcome = _object(raw_outcome, "outcome")
            participant_id = _identifier(
                outcome.get("participantId"),
                "participantId",
            )
            if (
                participant_id not in participant_set
                or participant_id in case_archetypes
            ):
                raise MarketExperimentError(
                    "outcome participant must occur once in the frozen roster"
                )
            archetype = outcome.get("archetype")
            if archetype not in _ARCHETYPES:
                raise MarketExperimentError(
                    "outcome archetype is not supported"
                )
            net_worth = _non_negative_atomic(
                outcome.get("netWorthAtomic"),
                "netWorthAtomic",
            )
            return_bps = (
                (net_worth - INITIAL_NET_WORTH_ATOMIC) * 10_000
                // INITIAL_NET_WORTH_ATOMIC
            )
            case_archetypes[participant_id] = str(archetype)
            outcome_values.append(net_worth)
            outcome_returns.append(return_bps)
            outcomes_by_archetype.setdefault(str(archetype), []).append(
                return_bps
            )
            case_outcomes.append(
                {
                    "participantId": participant_id,
                    "archetype": archetype,
                    "netWorthAtomic": str(net_worth),
                }
            )
        raw_rounds = _array(case.get("rounds"), "rounds")
        if not raw_rounds:
            raise MarketExperimentError("a case requires at least one round")

        case_round_indexes: list[int] = []
        fingerprint_rounds: list[object] = []
        for raw_round in sorted(
            raw_rounds,
            key=lambda item: _positive_int(
                _object(item, "round").get("roundIndex"),
                "roundIndex",
            ),
        ):
            round_value = _object(raw_round, "round")
            round_index = _positive_int(
                round_value.get("roundIndex"),
                "roundIndex",
            )
            if round_index in case_round_indexes:
                raise MarketExperimentError(
                    "roundIndex must be unique within a case"
                )
            case_round_indexes.append(round_index)
            intents: list[LiquidityIntent] = []
            private_intents: list[object] = []
            for raw_intent in _array(
                round_value.get("intents", ()),
                "intents",
            ):
                intent = _object(raw_intent, "intent")
                participant_id = _identifier(
                    intent.get("participantId"),
                    "participantId",
                )
                if participant_id not in participant_set:
                    raise MarketExperimentError(
                        "intent participant is outside the frozen roster"
                    )
                side = intent.get("side")
                if side not in {"buy", "sell"}:
                    raise MarketExperimentError(
                        "intent side must be buy or sell"
                    )
                good = require_good(str(intent.get("good")))
                limit_price = _positive_int(
                    intent.get("limitPriceAtomic"),
                    "limitPriceAtomic",
                )
                intents.append(
                    LiquidityIntent(
                        participant_id=participant_id,
                        side=side,
                        good=good,
                        limit_price_atomic=limit_price,
                    )
                )
                private_intents.append(
                    {
                        "participantId": participant_id,
                        "side": side,
                        "good": good,
                        "limitPriceAtomic": str(limit_price),
                    }
                )

            summary = summarize_round_liquidity(
                participant_count=len(participant_ids),
                intents=tuple(intents),
            )
            payload = summary.to_public_payload()
            totals["participantRoundCount"] += len(participant_ids)
            totals["intentCount"] += summary.intent_count
            totals["passCount"] += summary.pass_count
            totals["oppositeSideCapacity"] += summary.opposite_side_capacity
            totals[
                "priceCompatibleCapacity"
            ] += summary.price_compatible_capacity
            if summary.opposite_side_capacity:
                opposite_round_count += 1
            if summary.price_compatible_capacity:
                compatible_round_count += 1
            for good in GOOD_IDS:
                public_good = payload["byGood"][good]  # type: ignore[index]
                for field in (
                    "buyIntentCount",
                    "sellIntentCount",
                    "oppositeSideCapacity",
                    "priceCompatibleCapacity",
                ):
                    value = int(public_good[field])
                    by_good[good][field] += value
                    if field in {"buyIntentCount", "sellIntentCount"}:
                        totals[field] += value

            funnel = _object(round_value.get("funnel", {}), "funnel")
            funnel_values = {
                field: _non_negative_int(funnel.get(field, 0), field)
                for field in _FUNNEL_FIELDS
            }
            if not (
                funnel_values["inventoryCommittedCount"]
                <= funnel_values["chainConfirmedCount"]
                <= funnel_values["dealCount"]
                <= funnel_values["engagementCount"]
                <= funnel_values["rfqCount"]
            ):
                raise MarketExperimentError(
                    "funnel counts must be monotonically non-increasing"
                )
            if (
                funnel_values["counterOfferCount"]
                > funnel_values["negotiationActionCount"]
            ):
                raise MarketExperimentError(
                    "counter offers cannot exceed negotiation actions"
                )
            if (
                funnel_values["counterpartyParticipantCount"]
                > len(participant_ids)
            ):
                raise MarketExperimentError(
                    "counterparty participants cannot exceed the roster"
                )
            for field, value in funnel_values.items():
                totals[field] += value

            round_count += 1
            fingerprint_rounds.append(
                {
                    "roundIndex": round_index,
                    "intents": sorted(
                        private_intents,
                        key=lambda value: str(value["participantId"]),
                    ),
                    "funnel": funnel_values,
                }
            )

        design_cases.append(
            {
                "caseId": case_id,
                "eventSeed": event_seed,
                "participantIds": sorted(participant_ids),
                "archetypes": {
                    participant_id: case_archetypes[participant_id]
                    for participant_id in sorted(participant_ids)
                },
                "roundIndexes": case_round_indexes,
            }
        )
        private_fingerprint_cases.append(
            {
                "caseId": case_id,
                "eventSeed": event_seed,
                "participantIds": sorted(participant_ids),
                "outcomes": sorted(
                    case_outcomes,
                    key=lambda value: str(value["participantId"]),
                ),
                "rounds": fingerprint_rounds,
            }
        )

    outcomes = {
        "participantCount": len(outcome_values),
        "meanNetWorthAtomicFloor": str(
            sum(outcome_values) // len(outcome_values)
        ),
        "minimumNetWorthAtomic": str(min(outcome_values)),
        "meanReturnBps": sum(outcome_returns) // len(outcome_returns),
        "downsideReturnBps": min(outcome_returns),
        "byArchetype": {
            archetype: {
                "participantCount": len(values),
                "meanReturnBps": sum(values) // len(values),
                "downsideReturnBps": min(values),
            }
            for archetype, values in sorted(outcomes_by_archetype.items())
        },
    }
    rates = {
        "intentRate": _ratio(
            totals["intentCount"],
            totals["participantRoundCount"],
        ),
        "oppositeSideRoundRate": _ratio(
            opposite_round_count,
            round_count,
        ),
        "compatibleRoundRate": _ratio(
            compatible_round_count,
            round_count,
        ),
        "rfqToEngagementRate": _ratio(
            totals["engagementCount"],
            totals["rfqCount"],
        ),
        "engagementToDealRate": _ratio(
            totals["dealCount"],
            totals["engagementCount"],
        ),
        "dealToChainConfirmedRate": _ratio(
            totals["chainConfirmedCount"],
            totals["dealCount"],
        ),
        "chainConfirmedToInventoryCommittedRate": _ratio(
            totals["inventoryCommittedCount"],
            totals["chainConfirmedCount"],
        ),
        "invalidActionRate": _ratio(
            totals["invalidActionCount"],
            totals["participantRoundCount"],
        ),
        "defaultActionRate": _ratio(
            totals["defaultActionCount"],
            totals["participantRoundCount"],
        ),
        "invalidStructuredOutputRate": _ratio(
            totals["invalidStructuredOutputCount"],
            totals["participantRoundCount"],
        ),
        "timeoutRate": _ratio(
            totals["timeoutCount"],
            totals["participantRoundCount"],
        ),
        "counterOfferRate": _ratio(
            totals["counterOfferCount"],
            totals["negotiationActionCount"],
        ),
        "averageNegotiationActionsPerEngagement": _ratio(
            totals["negotiationActionCount"],
            totals["engagementCount"],
        ),
        "counterpartyCoverageRate": _ratio(
            totals["counterpartyParticipantCount"],
            totals["participantRoundCount"],
        ),
        "directionEntropyBits": _entropy(
            (
                totals["buyIntentCount"],
                totals["sellIntentCount"],
                totals["passCount"],
            )
        ),
    }
    report = {
        "armId": arm_id,
        "caseCount": len(raw_cases),
        "roundCount": round_count,
        "inputFingerprint": _fingerprint(private_fingerprint_cases),
        "totals": totals,
        "byGood": by_good,
        "rates": rates,
        "outcomes": outcomes,
    }
    return report, design_cases


def _delta(
    treatment: int | float | None,
    control: int | float | None,
) -> int | float | None:
    if treatment is None or control is None:
        return None
    value = treatment - control
    return round(value, 6) if isinstance(value, float) else value


def evaluate_market_quality_experiment(
    manifest: Mapping[str, object],
) -> dict[str, object]:
    """Evaluate paired offline arms without returning participant-level data."""

    if manifest.get("schemaVersion") != EXPERIMENT_SCHEMA_VERSION:
        raise MarketExperimentError("unsupported experiment schema version")
    experiment_id = _identifier(manifest.get("experimentId"), "experimentId")
    control, control_design = _arm_report(
        _object(manifest.get("control"), "control")
    )
    treatment, treatment_design = _arm_report(
        _object(manifest.get("treatment"), "treatment")
    )
    if control["armId"] == treatment["armId"]:
        raise MarketExperimentError("control and treatment arm IDs must differ")
    if control_design != treatment_design:
        raise MarketExperimentError(
            "control and treatment must use the same paired design"
        )

    control_totals = control["totals"]
    treatment_totals = treatment["totals"]
    control_rates = control["rates"]
    treatment_rates = treatment["rates"]
    delta_fields = (
        "intentCount",
        "passCount",
        "oppositeSideCapacity",
        "priceCompatibleCapacity",
        "rfqCount",
        "engagementCount",
        "dealCount",
        "chainConfirmedCount",
        "inventoryCommittedCount",
        "invalidActionCount",
        "defaultActionCount",
        "invalidStructuredOutputCount",
        "timeoutCount",
    )
    rate_fields = (
        "intentRate",
        "oppositeSideRoundRate",
        "compatibleRoundRate",
        "rfqToEngagementRate",
        "engagementToDealRate",
        "dealToChainConfirmedRate",
        "invalidActionRate",
        "defaultActionRate",
        "invalidStructuredOutputRate",
        "timeoutRate",
        "counterOfferRate",
        "directionEntropyBits",
    )
    deltas: dict[str, int | float | None] = {
        field: _delta(
            treatment_totals[field],  # type: ignore[index]
            control_totals[field],  # type: ignore[index]
        )
        for field in delta_fields
    }
    deltas.update(
        {
            field: _delta(
                treatment_rates[field],  # type: ignore[index]
                control_rates[field],  # type: ignore[index]
            )
            for field in rate_fields
        }
    )
    control_outcomes = control["outcomes"]
    treatment_outcomes = treatment["outcomes"]
    deltas.update(
        {
            "meanReturnBps": _delta(
                treatment_outcomes["meanReturnBps"],  # type: ignore[index]
                control_outcomes["meanReturnBps"],  # type: ignore[index]
            ),
            "downsideReturnBps": _delta(
                treatment_outcomes["downsideReturnBps"],  # type: ignore[index]
                control_outcomes["downsideReturnBps"],  # type: ignore[index]
            ),
        }
    )
    return {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "experimentId": experiment_id,
        "status": "paired",
        "designFingerprint": _fingerprint(control_design),
        "control": control,
        "treatment": treatment,
        "deltas": deltas,
    }


__all__ = [
    "EXPERIMENT_SCHEMA_VERSION",
    "MarketExperimentError",
    "REPORT_SCHEMA_VERSION",
    "evaluate_market_quality_experiment",
]
