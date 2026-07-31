"""Arena-owned task broker exposed through the stateless MCP adapter."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any, Protocol

from arena_agent_contracts import AgentTaskResultV1
from arena_core import ArenaResultSink, ConnectorTaskRoute
from arena_core.models import ResultSubmissionReceipt

from .auth import ExecutionTokenClaims, ExecutionTokenError


class ArenaMCPBrokerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ArenaMCPTaskRepository(Protocol):
    async def claim_connector_task(
        self,
        *,
        task_id: str,
        connector_binding_id: str,
        connector_binding_epoch: int,
        worker_id: str,
        lease_seconds: int,
    ) -> ConnectorTaskRoute | None: ...

    async def get_connector_task_route(
        self,
        *,
        task_id: str,
        connector_binding_id: str,
        connector_binding_epoch: int,
    ) -> ConnectorTaskRoute | None: ...

    async def list_connector_task_routes(
        self,
        *,
        connector_binding_id: str,
        connector_binding_epoch: int,
        after_task_id: str | None,
        limit: int,
    ) -> Sequence[ConnectorTaskRoute]: ...

    async def release_connector_task(
        self,
        *,
        task_id: str,
        worker_id: str,
    ) -> bool: ...

    async def get_result_for_task(self, task_id: str) -> Any: ...


class ArenaMCPGateway(Protocol):
    async def list_bindings(
        self,
        device_id: str | None = None,
    ) -> list[dict[str, Any]]: ...


class ArenaTaskBroker:
    """Authorize explicit task handles and delegate terminal ingress to Arena."""

    def __init__(
        self,
        *,
        repository: ArenaMCPTaskRepository,
        result_sink: ArenaResultSink,
        gateway: ArenaMCPGateway,
        lease_seconds: int = 600,
    ) -> None:
        if lease_seconds < 1 or lease_seconds > 600:
            raise ValueError("Arena MCP lease must be between 1 and 600 seconds")
        self._repository = repository
        self._result_sink = result_sink
        self._gateway = gateway
        self._lease_seconds = lease_seconds

    async def claim(
        self,
        *,
        principal: ExecutionTokenClaims,
        task_id: str,
    ) -> dict[str, Any]:
        self._require(principal, "task:claim")
        binding = await self._binding(principal)
        route = await self._repository.claim_connector_task(
            task_id=task_id,
            connector_binding_id=principal.binding_id,
            connector_binding_epoch=principal.binding_epoch,
            worker_id=principal.worker_id,
            lease_seconds=self._lease_seconds,
        )
        if route is None:
            existing = await self._repository.get_connector_task_route(
                task_id=task_id,
                connector_binding_id=principal.binding_id,
                connector_binding_epoch=principal.binding_epoch,
            )
            if existing is None:
                raise ArenaMCPBrokerError(
                    "task_not_found",
                    "Task is not assigned to this execution binding",
                )
            if existing.status.terminal:
                raise ArenaMCPBrokerError(
                    "task_terminal",
                    "Task is already terminal; inspect its status instead",
                )
            raise ArenaMCPBrokerError(
                "task_unavailable",
                "Task is not currently claimable; retry after the active lease expires",
            )
        session_id = str(binding.get("last_session_id") or "").strip()
        if not session_id:
            await self._repository.release_connector_task(
                task_id=task_id,
                worker_id=principal.worker_id,
            )
            raise ArenaMCPBrokerError(
                "managed_session_unavailable",
                "The Connector-owned runtime session is not ready yet",
            )
        return {
            "leaseId": principal.worker_id,
            "task": route.task.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            ),
            "execution": {
                "bindingId": principal.binding_id,
                "bindingEpoch": principal.binding_epoch,
                "agentId": str(binding["agent_id"]),
                "runtimeId": str(binding["runtime_id"]),
                "sessionId": session_id,
            },
        }

    async def status(
        self,
        *,
        principal: ExecutionTokenClaims,
        task_id: str,
    ) -> dict[str, Any]:
        self._require(principal, "task:read")
        await self._binding(principal)
        route = await self._authorized_route(principal, task_id)
        result = await self._repository.get_result_for_task(task_id)
        return {
            "taskId": task_id,
            "status": route.status.value,
            "deadlineAt": route.task.deadline_at.isoformat().replace("+00:00", "Z"),
            "leaseExpiresAt": (
                None
                if route.lease_expires_at is None
                else route.lease_expires_at.isoformat().replace("+00:00", "Z")
            ),
            "hasAuthoritativeResult": result is not None,
            "resultDisposition": (
                None
                if result is None
                else getattr(
                    getattr(result, "apply_status", "pending"),
                    "value",
                    getattr(result, "apply_status", "pending"),
                )
            ),
        }

    async def submit(
        self,
        *,
        principal: ExecutionTokenClaims,
        result: AgentTaskResultV1,
    ) -> dict[str, Any]:
        self._require(principal, "task:submit")
        await self._binding(principal)
        await self._authorized_route(principal, result.task_id)
        receipt = await self._result_sink.submit(result)
        return _receipt_payload(receipt, result.result_id)

    async def release(
        self,
        *,
        principal: ExecutionTokenClaims,
        task_id: str,
    ) -> dict[str, Any]:
        self._require(principal, "task:release")
        await self._binding(principal)
        await self._authorized_route(principal, task_id)
        released = await self._repository.release_connector_task(
            task_id=task_id,
            worker_id=principal.worker_id,
        )
        if not released:
            raise ArenaMCPBrokerError(
                "lease_not_owned",
                "The current execution binding does not own an active task lease",
            )
        return {"taskId": task_id, "released": True}

    async def sync(
        self,
        *,
        principal: ExecutionTokenClaims,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        self._require(principal, "task:read")
        await self._binding(principal)
        after_task_id = _decode_cursor(cursor) if cursor else None
        routes = list(
            await self._repository.list_connector_task_routes(
                connector_binding_id=principal.binding_id,
                connector_binding_epoch=principal.binding_epoch,
                after_task_id=after_task_id,
                limit=limit + 1,
            )
        )
        has_more = len(routes) > limit
        selected = routes[:limit]
        return {
            "tasks": [
                {
                    "taskId": route.task.task_id,
                    "bindingId": route.connector_binding_id,
                    "bindingEpoch": route.connector_binding_epoch,
                    "deadlineAt": route.task.deadline_at.isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "status": route.status.value,
                }
                for route in selected
            ],
            "hasMore": has_more,
            "nextCursor": (
                _encode_cursor(selected[-1].task.task_id)
                if has_more and selected
                else None
            ),
        }

    async def _authorized_route(
        self,
        principal: ExecutionTokenClaims,
        task_id: str,
    ) -> ConnectorTaskRoute:
        route = await self._repository.get_connector_task_route(
            task_id=task_id,
            connector_binding_id=principal.binding_id,
            connector_binding_epoch=principal.binding_epoch,
        )
        if route is None:
            raise ArenaMCPBrokerError(
                "task_not_found",
                "Task is not assigned to this execution binding",
            )
        return route

    async def _binding(
        self,
        principal: ExecutionTokenClaims,
    ) -> dict[str, Any]:
        binding = next(
            (
                item
                for item in await self._gateway.list_bindings(principal.device_id)
                if item.get("binding_id") == principal.binding_id
            ),
            None,
        )
        if binding is None:
            raise ArenaMCPBrokerError(
                "binding_not_found",
                "Execution binding is no longer available",
            )
        if int(binding.get("binding_epoch", 0)) != principal.binding_epoch:
            raise ArenaMCPBrokerError(
                "stale_binding_epoch",
                "Execution token uses a stale binding epoch",
            )
        return binding

    @staticmethod
    def _require(principal: ExecutionTokenClaims, scope: str) -> None:
        try:
            principal.require_scope(scope)
        except ExecutionTokenError as exc:
            raise ArenaMCPBrokerError(
                "insufficient_scope",
                "Execution token does not authorize this tool",
            ) from exc


def _receipt_payload(
    receipt: ResultSubmissionReceipt,
    submitted_result_id: str,
) -> dict[str, Any]:
    return {
        "taskId": receipt.task_id,
        "resultId": submitted_result_id,
        "disposition": receipt.disposition.value,
        "taskStatus": receipt.task_status.value,
        "authoritativeResultId": receipt.authoritative_result_id,
        "resultReceivedAt": receipt.result_received_at.isoformat().replace(
            "+00:00", "Z"
        ),
    }


def _encode_cursor(task_id: str) -> str:
    return base64.urlsafe_b64encode(task_id.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> str:
    if not cursor or len(cursor) > 512 or any(char.isspace() for char in cursor):
        raise ArenaMCPBrokerError("invalid_cursor", "Task cursor is invalid")
    try:
        padding = "=" * (-len(cursor) % 4)
        task_id = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        ).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ArenaMCPBrokerError(
            "invalid_cursor",
            "Task cursor is invalid",
        ) from exc
    if (
        not task_id
        or len(task_id) > 256
        or not task_id[0].isalnum()
        or any(not (char.isalnum() or char in "._:-") for char in task_id)
    ):
        raise ArenaMCPBrokerError("invalid_cursor", "Task cursor is invalid")
    return task_id


__all__ = [
    "ArenaMCPBrokerError",
    "ArenaMCPTaskRepository",
    "ArenaTaskBroker",
]
