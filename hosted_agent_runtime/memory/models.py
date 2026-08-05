"""Pydantic contracts for safe Agent continuity without raw reasoning."""

from __future__ import annotations

import re
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from arena_agent_contracts import AgentDrivenMarketActionV1


_SECRET_LIKE = re.compile(
    r"(?i)(?:api[_ -]?key|authorization|bearer|private[_ -]?key|seed phrase)"
)
SafeShortText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=500, strip_whitespace=True),
]
SafeFactor = Annotated[
    str,
    StringConstraints(min_length=1, max_length=160, strip_whitespace=True),
]
AgentActionT = TypeVar(
    "AgentActionT",
    bound=AgentDrivenMarketActionV1,
)


class _MemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @field_validator("*", mode="after")
    @classmethod
    def reject_secret_like_text(cls, value: object) -> object:
        values: list[str] = []
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str))
        if any(_SECRET_LIKE.search(item) for item in values):
            raise ValueError("memory text contains a forbidden secret-like label")
        return value


class SafeDecisionSummary(_MemoryModel):
    """Auditable factors, never private chain-of-thought."""

    plan: SafeShortText
    factors: Annotated[list[SafeFactor], Field(min_length=1, max_length=6)]
    confidence_bps: int = Field(ge=0, le=10_000)


class GameMemoryPatch(_MemoryModel):
    """A proposed private strategy-state update.

    The patch is staged before Result submission and committed only after the
    Arena Result Consumer applies the corresponding candidate action.
    """

    round_summary: SafeShortText
    next_plan: SafeShortText
    observations: Annotated[list[SafeFactor], Field(max_length=6)] = Field(
        default_factory=list
    )
    strategy_adjustments: Annotated[
        list[SafeFactor], Field(max_length=4)
    ] = Field(default_factory=list)
    risk_budget_bps: int = Field(ge=0, le=10_000)


class HostedGameMemory(_MemoryModel):
    schema_version: Literal["arena.hosted-game-memory.v1"] = (
        "arena.hosted-game-memory.v1"
    )
    memory_version: int = Field(ge=0)
    state: dict[str, object] = Field(default_factory=dict)


class HostedAgentRunOutput(_MemoryModel, Generic[AgentActionT]):
    action: AgentActionT
    decision_summary: SafeDecisionSummary
    memory_patch: GameMemoryPatch


__all__ = [
    "GameMemoryPatch",
    "HostedAgentRunOutput",
    "HostedGameMemory",
    "SafeDecisionSummary",
]
