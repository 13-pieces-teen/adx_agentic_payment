"""PydanticAI learner that studies one completed Arena game."""

from __future__ import annotations

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model

from .models import HostedLearningEvidence, StrategyLearningProposal


_LEARNING_INSTRUCTIONS = """
You are the post-game learner for one persistent Arena 402 Hosted Agent.
Inspect the verified outcome and behavior through the supplied read-only
tools, then propose one bounded numeric policy update for future games.

Do not rewrite the public archetype. Do not invent trades, prices, actions,
rankings, or causal claims. A single game is weak evidence: keep changes small,
state lessons as hypotheses, and prefer no directional overreaction. Never
return credentials, public messages, hidden reasoning, or chain-of-thought.
`confidenceBps` means confidence that the bounded candidate is safe and
consistent with the verified evidence, not confidence that it guarantees a
future profit. When causal evidence is weak, keep parameters unchanged or make
a smaller change and describe the lesson as a hypothesis; do not claim a
guaranteed economic effect.
The output is only a candidate; deterministic schema, safety, replay, and
staleness gates decide whether it can become active for a later game.
""".strip()


def inspect_verified_outcome(
    ctx: RunContext[HostedLearningEvidence],
) -> dict[str, object]:
    """Return ranking and final-price evidence from the completed game."""

    return {
        "gameId": ctx.deps.game_id,
        "outcome": ctx.deps.outcome.model_dump(
            mode="json",
            by_alias=True,
        ),
        "finalPricesAtomic": dict(ctx.deps.final_prices_atomic),
    }


def inspect_verified_behavior(
    ctx: RunContext[HostedLearningEvidence],
) -> dict[str, object]:
    """Return bounded action, settlement, usage, and prior-policy evidence."""

    return {
        "archetype": ctx.deps.archetype.value,
        "basePolicyProfile": ctx.deps.base_policy_profile.model_dump(
            mode="json",
            by_alias=True,
        ),
        "behavior": ctx.deps.behavior.model_dump(
            mode="json",
            by_alias=True,
        ),
        "lastGameMemory": dict(ctx.deps.last_game_memory),
    }


def build_strategy_learning_agent(
    model: Model,
) -> Agent[HostedLearningEvidence, StrategyLearningProposal]:
    agent = Agent(
        model,
        deps_type=HostedLearningEvidence,
        output_type=StrategyLearningProposal,
        instructions=_LEARNING_INSTRUCTIONS,
        retries=1,
        tools=(inspect_verified_outcome, inspect_verified_behavior),
        end_strategy="exhaustive",
    )

    @agent.output_validator
    def require_evidence_inspection(
        ctx: RunContext[HostedLearningEvidence],
        output: StrategyLearningProposal,
    ) -> StrategyLearningProposal:
        if ctx.usage.tool_calls < 2:
            raise ModelRetry(
                "inspect_outcome_and_behavior_before_proposing_strategy"
            )
        return output

    return agent


__all__ = [
    "build_strategy_learning_agent",
    "inspect_verified_behavior",
    "inspect_verified_outcome",
]
