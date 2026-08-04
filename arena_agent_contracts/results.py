"""Terminal runtime result contract for Arena Agent tasks."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from .market import AgentDrivenMarketActionV1
from .tasks import Identifier

AGENT_TASK_RESULT_SCHEMA_VERSION_V1: Final = "arena.agent-result.v1"


def _to_camel(field_name: str) -> str:
    head, *tail = field_name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class AgentTaskResultV1(BaseModel):
    """The one terminal candidate result a Runtime may submit.

    Usage, provider diagnostics and attempts are private execution records and
    are deliberately absent.  In particular, this contract has no field for
    private reasoning or chain-of-thought.
    """

    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        strict=True,
    )

    result_id: Identifier
    task_id: Identifier
    schema_version: Literal["arena.agent-result.v1"]
    status: Literal["succeeded", "failed", "timed_out", "cancelled"]
    action: AgentDrivenMarketActionV1 | None = None

    @model_validator(mode="after")
    def enforce_status_shape(self) -> "AgentTaskResultV1":
        if self.status == "succeeded" and self.action is None:
            raise ValueError("succeeded results require one structured action")
        if self.status != "succeeded" and self.action is not None:
            raise ValueError(f"{self.status} results must not include an action")
        return self


__all__ = [
    "AGENT_TASK_RESULT_SCHEMA_VERSION_V1",
    "AgentTaskResultV1",
]
