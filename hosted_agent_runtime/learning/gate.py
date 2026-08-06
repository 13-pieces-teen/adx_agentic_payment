"""Deterministic safety and replay gates for learned strategy candidates."""

from __future__ import annotations

from dataclasses import dataclass

from hosted_agent_runtime.strategy import StrategyArchetype

from .models import HostedLearningEvidence, StrategyLearningProposal


@dataclass(frozen=True, slots=True)
class LearningGateDecision:
    passed: bool
    reason: str
    evidence_summary: dict[str, object]


@dataclass(frozen=True, slots=True)
class LearningEvidenceDecision:
    passed: bool
    reason: str
    checks: dict[str, bool]


_ARCHETYPE_ENVELOPES: dict[
    StrategyArchetype,
    dict[str, tuple[int, int]],
] = {
    StrategyArchetype.AGGRESSIVE: {
        "risk_budget_bps": (5_000, 9_000),
        "min_expected_edge_bps": (0, 2_000),
        "max_inventory_concentration_bps": (5_000, 10_000),
        "negotiation_concession_bps": (700, 3_000),
        "exploration_bps": (1_000, 5_000),
    },
    StrategyArchetype.CONSERVATIVE: {
        "risk_budget_bps": (1_000, 5_000),
        "min_expected_edge_bps": (1_000, 5_000),
        "max_inventory_concentration_bps": (2_000, 7_000),
        "negotiation_concession_bps": (0, 1_500),
        "exploration_bps": (0, 2_000),
    },
    StrategyArchetype.BALANCED: {
        "risk_budget_bps": (3_000, 7_000),
        "min_expected_edge_bps": (500, 3_000),
        "max_inventory_concentration_bps": (4_000, 8_500),
        "negotiation_concession_bps": (300, 2_200),
        "exploration_bps": (500, 3_000),
    },
    StrategyArchetype.CUSTOM: {
        "risk_budget_bps": (1_000, 9_000),
        "min_expected_edge_bps": (0, 5_000),
        "max_inventory_concentration_bps": (2_000, 10_000),
        "negotiation_concession_bps": (0, 3_000),
        "exploration_bps": (0, 5_000),
    },
}
_MAX_SINGLE_GAME_DELTA_BPS = 1_000
_REQUIRED_FINAL_GOODS = {"grain", "iron", "warhorse", "gems"}


def evaluate_learning_evidence(
    evidence: HostedLearningEvidence,
) -> LearningEvidenceDecision:
    evidence_complete = (
        set(evidence.final_prices_atomic) == _REQUIRED_FINAL_GOODS
        and evidence.outcome.rank <= evidence.outcome.participant_count
        and evidence.behavior.task_count
        >= evidence.behavior.candidate_action_count
        and evidence.behavior.candidate_action_count >= 1
    )
    replay_count = sum(evidence.behavior.applied_action_counts.values())
    active_action_count = sum(
        count
        for action, count in evidence.behavior.applied_action_counts.items()
        if action not in {"pass", "reject", "reject_all"}
    )
    replay_passed = (
        replay_count >= evidence.behavior.candidate_action_count
        and replay_count <= evidence.behavior.task_count
        and evidence.behavior.defaulted_task_count
        + evidence.behavior.candidate_action_count
        <= evidence.behavior.task_count
    )
    multi_step_passed = evidence.behavior.task_count >= 2
    market_participation_passed = active_action_count >= 1
    economic_outcome_passed = (
        evidence.outcome.net_worth_atomic
        != evidence.outcome.average_net_worth_atomic
        and evidence.behavior.settled_trade_count >= 1
    )
    checks = {
        "evidenceComplete": evidence_complete,
        "replayContractPassed": replay_passed,
        "multiStepEvidencePassed": multi_step_passed,
        "marketParticipationPassed": market_participation_passed,
        "economicOutcomePassed": economic_outcome_passed,
    }
    reason = "passed"
    if not evidence_complete:
        reason = "incomplete_verified_evidence"
    elif not replay_passed:
        reason = "historical_action_replay_failed"
    elif not multi_step_passed:
        reason = "insufficient_multi_step_evidence"
    elif not market_participation_passed:
        reason = "no_active_market_participation"
    elif not economic_outcome_passed:
        reason = "no_economic_outcome_signal"
    return LearningEvidenceDecision(
        passed=all(checks.values()),
        reason=reason,
        checks=checks,
    )


def evaluate_learning_proposal(
    evidence: HostedLearningEvidence,
    proposal: StrategyLearningProposal,
) -> LearningGateDecision:
    profile = proposal.policy_profile
    baseline = evidence.base_policy_profile
    envelope = _ARCHETYPE_ENVELOPES[evidence.archetype]
    values = {
        name: int(getattr(profile, name))
        for name in envelope
    }
    deltas = {
        name: values[name] - int(getattr(baseline, name))
        for name in envelope
    }
    envelope_passed = all(
        lower <= values[name] <= upper
        for name, (lower, upper) in envelope.items()
    )
    bounded_delta_passed = all(
        abs(delta) <= _MAX_SINGLE_GAME_DELTA_BPS
        for delta in deltas.values()
    )
    evidence_decision = evaluate_learning_evidence(evidence)
    checks = {
        "evidenceComplete": evidence_decision.checks[
            "evidenceComplete"
        ],
        "archetypeEnvelopePassed": envelope_passed,
        "boundedDeltaPassed": bounded_delta_passed,
        "replayContractPassed": evidence_decision.checks[
            "replayContractPassed"
        ],
        "multiStepEvidencePassed": evidence_decision.checks[
            "multiStepEvidencePassed"
        ],
        "marketParticipationPassed": evidence_decision.checks[
            "marketParticipationPassed"
        ],
        "economicOutcomePassed": evidence_decision.checks[
            "economicOutcomePassed"
        ],
    }
    reason = "passed"
    for check, passed in checks.items():
        if not passed:
            reason = {
                "evidenceComplete": "incomplete_verified_evidence",
                "archetypeEnvelopePassed": "archetype_envelope_violation",
                "boundedDeltaPassed": "single_game_delta_too_large",
                "replayContractPassed": "historical_action_replay_failed",
                "multiStepEvidencePassed": (
                    "insufficient_multi_step_evidence"
                ),
                "marketParticipationPassed": (
                    "no_active_market_participation"
                ),
                "economicOutcomePassed": "no_economic_outcome_signal",
            }[check]
            break
    summary: dict[str, object] = {
        "schemaVersion": "arena.hosted-learning-gate.v1",
        "gameId": evidence.game_id,
        "gameAgentId": evidence.game_agent_id,
        "baseStrategyRevisionId": evidence.base_strategy_revision_id,
        "outcomeScoreBps": evidence.outcome.outcome_score_bps,
        "rank": evidence.outcome.rank,
        "participantCount": evidence.outcome.participant_count,
        "candidateActionCount": evidence.behavior.candidate_action_count,
        "settledTradeCount": evidence.behavior.settled_trade_count,
        "checks": checks,
        "policyDeltaBps": deltas,
        "lessonSummary": proposal.lesson_summary,
        "expectedEffect": proposal.expected_effect,
        "confidenceBps": proposal.confidence_bps,
    }
    return LearningGateDecision(
        passed=all(checks.values()),
        reason=reason,
        evidence_summary=summary,
    )


__all__ = [
    "LearningEvidenceDecision",
    "LearningGateDecision",
    "evaluate_learning_evidence",
    "evaluate_learning_proposal",
]
