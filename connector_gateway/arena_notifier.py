"""WSS wake notifications for the stateless Arena MCP task data plane."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Protocol

from arena_core import ConnectorTaskRoute

from .models import CommandAction
from .service import ConnectorError, ConnectorGateway


_LOGGER = logging.getLogger(__name__)


class ConnectorTaskWakeRepository(Protocol):
    async def list_connector_task_wakes(
        self,
        *,
        limit: int,
    ) -> Sequence[ConnectorTaskRoute]: ...


class ConnectorArenaTaskNotifier:
    """Wake an online Connector without making WSS the task authority."""

    def __init__(
        self,
        *,
        repository: ConnectorTaskWakeRepository,
        gateway: ConnectorGateway,
        resend_seconds: float = 5.0,
        monotonic: Callable[[], float] | None = None,
        manage_sessions: bool = True,
    ) -> None:
        if resend_seconds < 1 or resend_seconds > 60:
            raise ValueError("Connector wake resend interval is invalid")
        self._repository = repository
        self._gateway = gateway
        self._resend_seconds = resend_seconds
        self._monotonic = monotonic or time.monotonic
        self._manage_sessions = manage_sessions
        self._last_sent: dict[str, float] = {}
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run_once(self, *, limit: int = 100) -> int:
        routes = await self._repository.list_connector_task_wakes(limit=limit)
        bindings = {
            str(binding["binding_id"]): binding
            for binding in await self._gateway.list_bindings()
        }
        active_wakes: set[str] = set()
        sent = 0
        for route in routes:
            wake_id = _wake_id(route)
            active_wakes.add(wake_id)
            now = self._monotonic()
            previous = self._last_sent.get(wake_id)
            if previous is not None and now - previous < self._resend_seconds:
                continue
            binding = bindings.get(route.connector_binding_id)
            if (
                binding is None
                or int(binding.get("binding_epoch", 0)) != route.connector_binding_epoch
            ):
                continue
            if not str(binding.get("last_session_id") or "").strip():
                if self._manage_sessions and await self._ensure_managed_session(
                    route,
                    binding,
                ):
                    sent += 1
                continue
            delivered = await self._gateway.notify_task_available(
                route.connector_binding_id,
                {
                    "wake_id": wake_id,
                    "task_id": route.task.task_id,
                    "binding_id": route.connector_binding_id,
                    "binding_epoch": route.connector_binding_epoch,
                    "deadline_at": route.task.deadline_at.isoformat().replace(
                        "+00:00", "Z"
                    ),
                },
            )
            if delivered:
                self._last_sent[wake_id] = now
                sent += 1
        self._last_sent = {
            wake_id: sent_at
            for wake_id, sent_at in self._last_sent.items()
            if wake_id in active_wakes
        }
        return sent

    async def run_forever(self, *, poll_seconds: float = 0.25) -> None:
        if poll_seconds <= 0:
            raise ValueError("Connector notifier poll interval must be positive")
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.error("connector_arena_notifier_cycle_failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=poll_seconds,
                )
            except TimeoutError:
                pass

    async def _ensure_managed_session(
        self,
        route: ConnectorTaskRoute,
        binding: dict[str, object],
    ) -> bool:
        working_directory = str(binding.get("working_directory") or "").strip()
        if not working_directory:
            return False
        remaining = (
            route.task.deadline_at - datetime.now(timezone.utc)
        ).total_seconds()
        if remaining <= 0:
            return False
        try:
            await self._gateway.queue_command(
                route.connector_binding_id,
                CommandAction.SESSION_START,
                {"working_directory": working_directory},
                (
                    "arena-session:"
                    f"{route.connector_binding_id}:"
                    f"{route.connector_binding_epoch}:"
                    f"{int(binding.get('session_generation', 0))}"
                ),
                max(1, min(3600, math.ceil(remaining))),
            )
        except ConnectorError:
            return False
        return True

def _wake_id(route: ConnectorTaskRoute) -> str:
    return f"wake:{route.task.task_id}:" f"{route.connector_binding_epoch}"


__all__ = [
    "ConnectorArenaTaskNotifier",
    "ConnectorTaskWakeRepository",
]
