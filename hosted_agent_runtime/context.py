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

    def __post_init__(self) -> None:
        if (
            not self.agent_id
            or not self.strategy_revision_id
            or self.strategy_revision_no < 1
            or not self.strategy_catalog_version
            or not self.strategy_instructions
        ):
            raise ValueError("Hosted Agent context is incomplete")
        if self.task.game_agent_id == "":
            raise ValueError("Hosted Agent task identity is missing")


__all__ = ["HostedArenaAgentContext"]
