"""Typed PydanticAI dependencies for one immutable Arena task."""

from __future__ import annotations

from dataclasses import dataclass, field

from arena_agent_contracts import ArenaAgentTaskV1

from .memory import HostedGameMemory
from .strategy import StrategyArchetype


@dataclass(frozen=True, slots=True)
class HostedArenaAgentContext:
    task: ArenaAgentTaskV1
    agent_id: str
    strategy_revision_id: str
    strategy_revision_no: int
    strategy_archetype: StrategyArchetype
    strategy_catalog_version: str
    strategy_instructions: str = field(repr=False)
    game_memory: HostedGameMemory
    analysis_tool_calls: set[str] = field(
        default_factory=set,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if (
            not self.agent_id
            or not self.strategy_revision_id
            or self.strategy_revision_no < 1
            or not self.strategy_catalog_version
        ):
            raise ValueError("Hosted Agent context is incomplete")
        # Owner strategy instructions are optional in the Hosted control-plane
        # contract.  An empty frozen revision means "no extra owner guidance";
        # the Arena system/task instructions still provide the bounded policy.
        if self.task.game_agent_id == "":
            raise ValueError("Hosted Agent task identity is missing")


__all__ = ["HostedArenaAgentContext"]
