"""Arena-owned typed task dispatch through a frozen Connector route."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from arena_agent_contracts import ArenaAgentTaskV1

from .models import CommandAction
from .service import ConnectorError, ConnectorGateway


@dataclass(frozen=True, slots=True)
class ConnectorArenaRoute:
    connector_binding_id: str
    binding_epoch: int


class ConnectorArenaRuntimeAdapter:
    """Translate an immutable Arena task into one typed Connector command.

    This adapter does not interpret command acknowledgements as business
    results. Terminal results return through ``agent_task.result`` and the
    Arena Result Sink.
    """

    def __init__(
        self,
        gateway: ConnectorGateway,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._gateway = gateway
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def dispatch(
        self,
        *,
        task: ArenaAgentTaskV1,
        route: ConnectorArenaRoute,
    ) -> dict[str, object]:
        binding = next(
            (
                item
                for item in await self._gateway.list_bindings()
                if item["binding_id"] == route.connector_binding_id
            ),
            None,
        )
        if binding is None:
            raise ConnectorError(404, "Connector binding not found")
        if int(binding.get("binding_epoch", 0)) != route.binding_epoch:
            raise ConnectorError(409, "Stale Connector binding epoch")
        session_id = str(binding.get("last_session_id") or "")
        if not session_id:
            raise ConnectorError(
                409,
                "Connector binding has no active Arena session",
            )

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Connector Arena adapter clock must be timezone-aware")
        remaining = (task.deadline_at - now).total_seconds()
        if remaining <= 0:
            raise ConnectorError(409, "Arena task deadline has expired")
        expires_in_seconds = max(1, min(3600, math.ceil(remaining)))
        return await self._gateway.queue_command(
            route.connector_binding_id,
            CommandAction.TASK_DISPATCH,
            {
                "session_id": session_id,
                "task": task.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=False,
                ),
            },
            task.idempotency_key,
            expires_in_seconds,
        )


__all__ = [
    "ConnectorArenaRoute",
    "ConnectorArenaRuntimeAdapter",
]
