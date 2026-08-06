from __future__ import annotations

import asyncio

from pydantic_ai import models
from pydantic_ai.models.test import TestModel

from hosted_agent_runtime.learning import (
    HostedLearningEvidence,
    HostedLearningRuntimeLimits,
    HostedStrategyLearningRuntime,
    LearningBehaviorSummary,
    LearningOutcome,
    StrategyLearningProposal,
    StrategyPolicyProfile,
    default_policy_profile,
    evaluate_learning_evidence,
    evaluate_learning_proposal,
    render_learned_strategy_instructions,
)
from hosted_agent_runtime.learning.runtime import (
    _unexpected_model_behavior_code,
)
from hosted_agent_runtime.strategy import StrategyArchetype


models.ALLOW_MODEL_REQUESTS = False


def test_learning_runtime_default_budget_covers_multi_step_tool_run() -> None:
    limits = HostedLearningRuntimeLimits()

    assert limits.request_limit == 4
    assert limits.tool_calls_limit == 4
    assert limits.output_tokens_limit == 8_192


def test_learning_runtime_classifies_safe_failure_reasons() -> None:
    assert (
        _unexpected_model_behavior_code(
            "Exceeded maximum output retries (1)"
        )
        == "output_retry_exhausted"
    )
    assert (
        _unexpected_model_behavior_code(
            "Model token limit (2048) exceeded before any response"
        )
        == "token_limit"
    )


def _evidence(
    *,
    archetype: StrategyArchetype = StrategyArchetype.BALANCED,
) -> HostedLearningEvidence:
    return HostedLearningEvidence(
        learning_job_id="learning:test-1",
        game_id="game-1",
        game_agent_id="game-agent-1",
        agent_id="agent-1",
        base_strategy_revision_id="strategy:test-1",
        base_strategy_revision_no=1,
        archetype=archetype,
        catalog_version="arena.hosted-strategy.v1",
        base_strategy_instructions=(
            "Stable numeric strategy: use eventImpliedFinal as fairValue and "
            "only submit legal, executable trades."
        ),
        base_policy_profile=default_policy_profile(archetype),
        outcome=LearningOutcome(
            rank=2,
            participant_count=10,
            net_worth_atomic="21000000",
            average_net_worth_atomic="20000000",
            outcome_score_bps=3889,
        ),
        behavior=LearningBehaviorSummary(
            task_count=12,
            candidate_action_count=10,
            defaulted_task_count=2,
            rejected_result_count=0,
            settled_trade_count=2,
            settlement_failure_count=0,
            applied_action_counts={
                "buy": 3,
                "sell": 2,
                "pass": 5,
                "propose": 2,
            },
            input_tokens=4_000,
            output_tokens=900,
            reasoning_tokens=400,
        ),
        final_prices_atomic={
            "grain": "2000000",
            "iron": "5000000",
            "warhorse": "8000000",
            "gems": "3000000",
        },
        last_game_memory={
            "schemaVersion": "arena.hosted-game-memory.v1",
            "latestPatch": {
                "next_plan": "Keep more liquidity for the final round."
            },
        },
    )


def _proposal(
    *,
    profile: StrategyPolicyProfile | None = None,
    confidence_bps: int = 7_500,
) -> StrategyLearningProposal:
    return StrategyLearningProposal(
        policy_profile=profile
        or StrategyPolicyProfile(
            risk_budget_bps=5_300,
            min_expected_edge_bps=1_000,
            max_inventory_concentration_bps=7_200,
            negotiation_concession_bps=1_100,
            exploration_bps=1_300,
        ),
        lesson_summary=(
            "The result supports a small liquidity adjustment, not a new "
            "public archetype."
        ),
        adjustments=[
            "Preserve slightly more cash before the final event.",
            "Require a modestly clearer edge for concentrated inventory.",
        ],
        expected_effect=(
            "Reduce concentration while preserving selective participation."
        ),
        confidence_bps=confidence_bps,
    )


def test_learning_gate_accepts_small_replayable_update() -> None:
    decision = evaluate_learning_proposal(_evidence(), _proposal())

    assert decision.passed is True
    assert decision.reason == "passed"
    assert decision.evidence_summary["checks"] == {
        "evidenceComplete": True,
        "archetypeEnvelopePassed": True,
        "boundedDeltaPassed": True,
        "replayContractPassed": True,
        "multiStepEvidencePassed": True,
        "marketParticipationPassed": True,
        "economicOutcomePassed": True,
    }


def test_learning_evidence_preflight_rejects_default_only_game() -> None:
    evidence = _evidence().model_copy(
        update={
            "behavior": LearningBehaviorSummary(
                task_count=10,
                candidate_action_count=0,
                defaulted_task_count=10,
                rejected_result_count=0,
                settled_trade_count=0,
                settlement_failure_count=0,
                applied_action_counts={},
                input_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
            )
        }
    )

    decision = evaluate_learning_evidence(evidence)

    assert decision.passed is False
    assert decision.reason == "incomplete_verified_evidence"
    assert decision.checks["evidenceComplete"] is False


def test_learning_evidence_preflight_rejects_single_task_game() -> None:
    evidence = _evidence().model_copy(
        update={
            "behavior": LearningBehaviorSummary(
                task_count=1,
                candidate_action_count=1,
                defaulted_task_count=0,
                rejected_result_count=0,
                settled_trade_count=0,
                settlement_failure_count=0,
                applied_action_counts={"buy": 1},
                input_tokens=100,
                output_tokens=30,
                reasoning_tokens=0,
            )
        }
    )

    decision = evaluate_learning_evidence(evidence)

    assert decision.passed is False
    assert decision.reason == "insufficient_multi_step_evidence"
    assert decision.checks["multiStepEvidencePassed"] is False


def test_learning_evidence_preflight_rejects_pass_only_game() -> None:
    evidence = _evidence().model_copy(
        update={
            "behavior": LearningBehaviorSummary(
                task_count=2,
                candidate_action_count=2,
                defaulted_task_count=0,
                rejected_result_count=0,
                settled_trade_count=0,
                settlement_failure_count=0,
                applied_action_counts={"pass": 2},
                input_tokens=200,
                output_tokens=60,
                reasoning_tokens=0,
            )
        }
    )

    decision = evaluate_learning_evidence(evidence)

    assert decision.passed is False
    assert decision.reason == "no_active_market_participation"
    assert decision.checks["marketParticipationPassed"] is False


def test_learning_evidence_preflight_rejects_tied_economic_outcome() -> None:
    evidence = _evidence().model_copy(
        update={
            "outcome": _evidence().outcome.model_copy(
                update={
                    "net_worth_atomic": "20000000",
                    "average_net_worth_atomic": "20000000",
                    "outcome_score_bps": 0,
                }
            ),
            "behavior": LearningBehaviorSummary(
                task_count=2,
                candidate_action_count=2,
                defaulted_task_count=0,
                rejected_result_count=0,
                settled_trade_count=1,
                settlement_failure_count=0,
                applied_action_counts={"buy": 1, "pass": 1},
                input_tokens=200,
                output_tokens=60,
                reasoning_tokens=0,
            ),
        }
    )

    decision = evaluate_learning_evidence(evidence)

    assert decision.passed is False
    assert decision.reason == "no_economic_outcome_signal"
    assert decision.checks["economicOutcomePassed"] is False


def test_learning_evidence_preflight_requires_settled_trade() -> None:
    evidence = _evidence().model_copy(
        update={
            "behavior": LearningBehaviorSummary(
                task_count=2,
                candidate_action_count=2,
                defaulted_task_count=0,
                rejected_result_count=0,
                settled_trade_count=0,
                settlement_failure_count=0,
                applied_action_counts={"buy": 1, "sell": 1},
                input_tokens=200,
                output_tokens=60,
                reasoning_tokens=0,
            )
        }
    )

    decision = evaluate_learning_evidence(evidence)

    assert decision.passed is False
    assert decision.reason == "no_economic_outcome_signal"
    assert decision.checks["economicOutcomePassed"] is False


def test_learning_gate_records_but_does_not_trust_model_confidence() -> None:
    decision = evaluate_learning_proposal(
        _evidence(),
        _proposal(confidence_bps=0),
    )

    assert decision.passed is True
    assert decision.evidence_summary["confidenceBps"] == 0


def test_learning_gate_rejects_single_game_overreaction() -> None:
    decision = evaluate_learning_proposal(
        _evidence(),
        _proposal(
            profile=StrategyPolicyProfile(
                risk_budget_bps=7_000,
                min_expected_edge_bps=500,
                max_inventory_concentration_bps=8_500,
                negotiation_concession_bps=2_000,
                exploration_bps=2_500,
            )
        ),
    )

    assert decision.passed is False
    assert decision.reason == "single_game_delta_too_large"


def test_learned_instructions_are_bounded_and_keep_archetype() -> None:
    proposal = _proposal()
    foundation = (
        "Stable numeric strategy: use eventImpliedFinal as fairValue, buy "
        "grain at or below 2.100000, and sell excess grain at or above "
        "1.900000."
    )
    value = render_learned_strategy_instructions(
        archetype=StrategyArchetype.BALANCED,
        base_strategy_instructions=foundation,
        profile=proposal.policy_profile,
        adjustments=proposal.adjustments,
    )

    assert "public archetype balanced" in value
    assert foundation in value
    assert "risk budget 5300 bps" in value
    assert "minimum expected edge 1000 bps" in value
    assert "never as permission" in value
    assert len(value) <= 4_000


def test_large_official_foundation_keeps_full_strategy_and_bounded_overlay():
    foundation = "F" * 3_249
    adjustments = [
        f"Lesson {index} " + ("x" * 165)
        for index in range(1, 5)
    ]

    value = render_learned_strategy_instructions(
        archetype=StrategyArchetype.BALANCED,
        base_strategy_instructions=foundation,
        profile=_proposal().policy_profile,
        adjustments=adjustments,
    )

    assert value.startswith(foundation)
    assert "public archetype balanced" in value
    assert "risk budget 5300 bps" in value
    assert adjustments[0] in value
    assert len(value) <= 4_000


def test_learning_runtime_can_inspect_tools_and_return_typed_proposal() -> None:
    proposal = _proposal()
    runtime = HostedStrategyLearningRuntime(
        model=TestModel(
            custom_output_args=proposal.model_dump(
                mode="json",
                by_alias=True,
            )
        ),
        actual_model="test-learning-model",
    )

    execution = asyncio.run(
        runtime.execute(_evidence(), timeout_seconds=10)
    )

    assert execution.status == "succeeded"
    assert execution.proposal == proposal
    assert execution.request_count >= 2
    assert execution.tool_call_count >= 2
    assert execution.usage.complete is True


def test_learning_runtime_accepts_typed_proposal_without_tool_calls() -> None:
    proposal = _proposal()
    runtime = HostedStrategyLearningRuntime(
        model=TestModel(
            call_tools=[],
            custom_output_args=proposal.model_dump(
                mode="json",
                by_alias=True,
            ),
        ),
        actual_model="test-learning-model",
    )

    execution = asyncio.run(
        runtime.execute(_evidence(), timeout_seconds=10)
    )

    assert execution.status == "succeeded"
    assert execution.proposal == proposal
    assert execution.request_count == 1
    assert execution.tool_call_count == 0
    assert execution.usage.complete is True
