"""Poll shared Connector Commands and route them to locally owned WSS sockets."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol


_LOGGER = logging.getLogger(__name__)


class SharedCommandGateway(Protocol):
    async def route_shared_commands_once(self, *, limit: int = 100) -> int: ...


class ConnectorSharedCommandRouter:
    def __init__(self, gateway: SharedCommandGateway) -> None:
        self._gateway = gateway
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run_once(self, *, limit: int = 100) -> int:
        return await self._gateway.route_shared_commands_once(limit=limit)

    async def run_forever(self, *, poll_seconds: float = 0.25) -> None:
        if poll_seconds <= 0:
            raise ValueError("Shared Command poll interval must be positive")
        while not self._stopping.is_set():
            try:
                delivered = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.error("connector_shared_command_route_cycle_failed")
                delivered = 0
            if delivered:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=poll_seconds,
                )
            except TimeoutError:
                pass


__all__ = ["ConnectorSharedCommandRouter"]
