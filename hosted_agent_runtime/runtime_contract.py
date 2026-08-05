"""Versioned control-plane constants for the PydanticAI Hosted Runtime."""

from typing import Final


HOSTED_AGENT_INSTRUCTION_VERSION_V1: Final[str] = (
    "arena.pydantic-agent.v1"
)
AGENT_ACTION_SCHEMA_VERSION_V1: Final[str] = "arena.agent-action.v1"
MAX_STRATEGY_BYTES: Final[int] = 4 * 1024


__all__ = [
    "AGENT_ACTION_SCHEMA_VERSION_V1",
    "HOSTED_AGENT_INSTRUCTION_VERSION_V1",
    "MAX_STRATEGY_BYTES",
]
