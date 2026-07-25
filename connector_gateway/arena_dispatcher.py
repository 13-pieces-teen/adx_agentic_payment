"""Database-backed dispatch of Arena tasks to connected local Runtimes."""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Protocol

from arena_core import ConnectorTaskClaim

from .arena_adapter import ConnectorArenaRoute, ConnectorArenaRuntimeAdapter
from .models import CommandAction
from .service import ConnectorError, ConnectorGateway


_LOGGER = logging.getLogger(__name__)


class ConnectorTaskClaimRepository(Protocol):
    async def claim_connector_tasks(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: int,
    ) -> Sequence[ConnectorTaskClaim]: ...

    async def defer_connector_task(
        self,
        *,
        task_id: str,
        worker_id: str,
        delay_seconds: int,
    ) -> None: ...


class ConnectorArenaTaskDispatcher:
    """Bridge leased Arena tasks into Connector-owned Sessions and Commands."""

    def __init__(
        self,
        *,
        repository: ConnectorTaskClaimRepository,
        gateway: ConnectorGateway,
        worker_id: str | None = None,
        lease_seconds: int = 5,
        retry_delay_seconds: int = 1,
    ) -> None:
        if lease_seconds < 1 or lease_seconds > 600:
            raise ValueError("Connector task lease must be between 1 and 600 seconds")
        if retry_delay_seconds < 1 or retry_delay_seconds > lease_seconds:
            raise ValueError("Connector task retry delay is outside the lease")
        self._repository = repository
        self._gateway = gateway
        self._adapter = ConnectorArenaRuntimeAdapter(gateway)
        self._worker_id = worker_id or (
            f"connector-dispatcher-{uuid.uuid4().hex[:12]}"
        )
        self._lease_seconds = lease_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run_once(self, *, limit: int = 25) -> int:
        claims = await self._repository.claim_connector_tasks(
            worker_id=self._worker_id,
            limit=limit,
            lease_seconds=self._lease_seconds,
        )
        processed = 0
        for claim in claims:
            await self._dispatch_claim(claim)
            processed += 1
        return processed

    async def run_forever(self, *, poll_seconds: float = 0.25) -> None:
        if poll_seconds <= 0:
            raise ValueError("Connector dispatcher poll interval must be positive")
        while not self._stopping.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.error("connector_arena_dispatcher_cycle_failed")
                processed = 0
            if processed:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=poll_seconds,
                )
            except TimeoutError:
                pass

    async def _dispatch_claim(self, claim: ConnectorTaskClaim) -> None:
        binding = next(
            (
                value
                for value in await self._gateway.list_bindings()
                if value["binding_id"] == claim.connector_binding_id
            ),
            None,
        )
        if (
            binding is None
            or int(binding.get("binding_epoch", 0))
            != claim.connector_binding_epoch
        ):
            await self._defer(claim)
            return

        session_id = str(binding.get("last_session_id") or "")
        if not session_id:
            working_directory = str(
                binding.get("working_directory") or ""
            ).strip()
            if not working_directory:
                await self._defer(claim)
                return
            remaining = (
                claim.task.deadline_at
                - datetime.now(timezone.utc)
            ).total_seconds()
            if remaining <= 0:
                await self._defer(claim)
                return
            try:
                await self._gateway.queue_command(
                    claim.connector_binding_id,
                    CommandAction.SESSION_START,
                    {"working_directory": working_directory},
                    (
                        "arena-session:"
                        f"{claim.connector_binding_id}:"
                        f"{claim.connector_binding_epoch}"
                    ),
                    max(1, min(3600, math.ceil(remaining))),
                )
            except ConnectorError:
                await self._defer(claim)
                return
            await self._defer(claim)
            return

        try:
            await self._adapter.dispatch(
                task=claim.task,
                route=ConnectorArenaRoute(
                    connector_binding_id=claim.connector_binding_id,
                    binding_epoch=claim.connector_binding_epoch,
                ),
            )
        except ConnectorError:
            await self._defer(claim)

    async def _defer(self, claim: ConnectorTaskClaim) -> None:
        await self._repository.defer_connector_task(
            task_id=claim.task.task_id,
            worker_id=self._worker_id,
            delay_seconds=self._retry_delay_seconds,
        )


__all__ = [
    "ConnectorArenaTaskDispatcher",
    "ConnectorTaskClaimRepository",
]
