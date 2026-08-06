"""Typed, bounded contracts for learning from completed Arena games."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from hosted_agent_runtime.strategy import (
    STRATEGY_CATALOG_VERSION_V1,
    StrategyArchetype,
    strategy_preset,
)


_SECRET_LIKE = re.compile(
    r"(?i)(?:api[_ -]?key|authorization|bearer|private[_ -]?key|seed phrase)"
)
SafeLearningText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=500, strip_whitespace=True),
]
SafeAdjustment = Annotated[
    str,
    StringConstraints(min_length=1, max_length=180, strip_whitespace=True),
]


class _LearningModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda name: (
            name.split("_")[0]
            + "".join(part.title() for part in name.split("_")[1:])
        ),
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        strict=True,
    )

    @field_validator("*", mode="after")
    @classmethod
    def reject_secret_like_text(cls, value: object) -> object:
        values: list[str] = []
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str))
        if any(_SECRET_LIKE.search(item) for item in values):
            raise ValueError("learning text contains a forbidden secret label")
        return value


class StrategyPolicyProfile(_LearningModel):
    """Small numeric policy surface that can be gated and replayed."""

    risk_budget_bps: int = Field(ge=1_000, le=9_000)
    min_expected_edge_bps: int = Field(ge=0, le=5_000)
    max_inventory_concentration_bps: int = Field(ge=2_000, le=10_000)
    negotiation_concession_bps: int = Field(ge=0, le=3_000)
    exploration_bps: int = Field(ge=0, le=5_000)


_DEFAULT_POLICY_PROFILES = {
    StrategyArchetype.AGGRESSIVE: StrategyPolicyProfile(
        risk_budget_bps=7_000,
        min_expected_edge_bps=400,
        max_inventory_concentration_bps=8_500,
        negotiation_concession_bps=1_800,
        exploration_bps=2_500,
    ),
    StrategyArchetype.CONSERVATIVE: StrategyPolicyProfile(
        risk_budget_bps=3_500,
        min_expected_edge_bps=1_500,
        max_inventory_concentration_bps=6_000,
        negotiation_concession_bps=700,
        exploration_bps=500,
    ),
    StrategyArchetype.BALANCED: StrategyPolicyProfile(
        risk_budget_bps=5_000,
        min_expected_edge_bps=900,
        max_inventory_concentration_bps=7_500,
        negotiation_concession_bps=1_200,
        exploration_bps=1_200,
    ),
    StrategyArchetype.CUSTOM: StrategyPolicyProfile(
        risk_budget_bps=5_000,
        min_expected_edge_bps=1_000,
        max_inventory_concentration_bps=7_500,
        negotiation_concession_bps=1_000,
        exploration_bps=1_000,
    ),
}


def default_policy_profile(
    archetype: StrategyArchetype | str,
) -> StrategyPolicyProfile:
    try:
        resolved = StrategyArchetype(archetype)
    except (TypeError, ValueError):
        raise ValueError("unsupported learning strategy archetype") from None
    return _DEFAULT_POLICY_PROFILES[resolved]


class LearningOutcome(_LearningModel):
    rank: int = Field(ge=1)
    participant_count: int = Field(ge=2, le=100)
    net_worth_atomic: Annotated[
        str,
        StringConstraints(pattern=r"^(?:0|[1-9][0-9]*)$"),
    ]
    average_net_worth_atomic: Annotated[
        str,
        StringConstraints(pattern=r"^(?:0|[1-9][0-9]*)$"),
    ]
    outcome_score_bps: int = Field(ge=-10_000, le=10_000)


class LearningBehaviorSummary(_LearningModel):
    task_count: int = Field(ge=0, le=10_000)
    candidate_action_count: int = Field(ge=0, le=10_000)
    defaulted_task_count: int = Field(ge=0, le=10_000)
    rejected_result_count: int = Field(ge=0, le=10_000)
    settled_trade_count: int = Field(ge=0, le=10_000)
    settlement_failure_count: int = Field(ge=0, le=10_000)
    applied_action_counts: dict[str, int] = Field(default_factory=dict)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)

    @field_validator("applied_action_counts")
    @classmethod
    def validate_action_counts(cls, value: dict[str, int]) -> dict[str, int]:
        allowed = {
            "buy",
            "sell",
            "pass",
            "propose",
            "accept",
            "reject",
            "request_negotiations",
            "engage",
            "reject_all",
        }
        if not set(value).issubset(allowed):
            raise ValueError("learning evidence contains an unknown action")
        if any(
            type(count) is not int or count < 0 or count > 10_000
            for count in value.values()
        ):
            raise ValueError("learning action count is invalid")
        return value


class HostedLearningEvidence(_LearningModel):
    schema_version: str = "arena.hosted-learning-evidence.v1"
    learning_job_id: SafeLearningText
    game_id: SafeLearningText
    game_agent_id: SafeLearningText
    agent_id: SafeLearningText
    base_strategy_revision_id: SafeLearningText
    base_strategy_revision_no: int = Field(ge=1)
    archetype: StrategyArchetype
    catalog_version: SafeLearningText
    base_policy_profile: StrategyPolicyProfile
    outcome: LearningOutcome
    behavior: LearningBehaviorSummary
    final_prices_atomic: dict[str, str] = Field(min_length=1, max_length=32)
    last_game_memory: dict[str, object] = Field(default_factory=dict)

    @field_validator("archetype", mode="before")
    @classmethod
    def parse_archetype(cls, value: object) -> StrategyArchetype:
        if isinstance(value, StrategyArchetype):
            return value
        if isinstance(value, str):
            return StrategyArchetype(value)
        raise ValueError("learning archetype is invalid")

    @field_validator("final_prices_atomic")
    @classmethod
    def validate_final_prices(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            not good
            or not isinstance(price, str)
            or not re.fullmatch(r"^(?:0|[1-9][0-9]*)$", price)
            for good, price in value.items()
        ):
            raise ValueError("learning final prices are invalid")
        return value


class StrategyLearningProposal(_LearningModel):
    policy_profile: StrategyPolicyProfile
    lesson_summary: SafeLearningText
    adjustments: Annotated[
        list[SafeAdjustment],
        Field(min_length=1, max_length=4),
    ]
    expected_effect: SafeLearningText
    confidence_bps: int = Field(ge=0, le=10_000)


def render_learned_strategy_instructions(
    *,
    archetype: StrategyArchetype,
    profile: StrategyPolicyProfile,
    adjustments: list[str],
) -> str:
    preset = strategy_preset(archetype)
    lessons = " ".join(
        f"Lesson {index}: {value.strip()}"
        for index, value in enumerate(adjustments, start=1)
    )
    rendered = (
        f"Strategy catalog {STRATEGY_CATALOG_VERSION_V1}; public archetype "
        f"{archetype.value}. {preset.instructions} "
        "Learned bounded policy: "
        f"risk budget {profile.risk_budget_bps} bps; "
        f"minimum expected edge {profile.min_expected_edge_bps} bps; "
        "maximum marked inventory concentration "
        f"{profile.max_inventory_concentration_bps} bps; "
        "total negotiation concession budget "
        f"{profile.negotiation_concession_bps} bps; "
        f"exploration budget {profile.exploration_bps} bps. "
        f"{lessons} "
        "Treat every value as a private preference, never as permission to "
        "violate Arena cash, inventory, price, deadline, settlement, privacy, "
        "or typed-action constraints."
    )
    if len(rendered) > 4_000:
        raise ValueError("learned strategy instructions exceed the limit")
    return rendered


__all__ = [
    "HostedLearningEvidence",
    "LearningBehaviorSummary",
    "LearningOutcome",
    "StrategyLearningProposal",
    "StrategyPolicyProfile",
    "default_policy_profile",
    "render_learned_strategy_instructions",
]
