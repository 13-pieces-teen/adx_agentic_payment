"""Arena-facing Runtime Driver extension point."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from .results import AgentTaskResultV1
from .tasks import ArenaAgentTaskV1


@runtime_checkable
class AgentRuntimeDriver(Protocol):
    """Execute exactly one immutable logical Arena task.

    Implementations may keep private attempt records, but they must return only
    the terminal public contract and must honor the Arena-owned absolute
    deadline.
    """

    async def execute(
        self,
        task_snapshot: ArenaAgentTaskV1,
        deadline: datetime,
    ) -> AgentTaskResultV1:
        """Return one terminal candidate result for ``task_snapshot``."""


__all__ = ["AgentRuntimeDriver"]
