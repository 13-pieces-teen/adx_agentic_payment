"""Arena-owned terminal result ingress."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from arena_agent_contracts import AgentTaskResultV1, ProposeAction, RejectAction

from .models import ResultSubmissionReceipt
from .ingress_security import validate_runtime_result_identifiers
from .public_output_policy import PublicOutputPolicy
from .repository import ArenaCoreRepository, ArenaTaskNotFoundError


class ArenaResultSink:
    def __init__(
        self,
        repository: ArenaCoreRepository,
        *,
        public_output_policy: PublicOutputPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._policy = public_output_policy or PublicOutputPolicy()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def submit(self, result: AgentTaskResultV1) -> ResultSubmissionReceipt:
        validate_runtime_result_identifiers(result)
        task = await self._repository.get_task(result.task_id)
        if task is None:
            raise ArenaTaskNotFoundError("Arena task does not exist")

        sanitized_result = result
        message_replaced = False
        policy_version: str | None = None
        action = result.action
        if isinstance(action, (ProposeAction, RejectAction)) and action.message:
            decision = self._policy.sanitize(
                message=action.message,
                action=action.action,
                price=getattr(action, "price", None),
                role=getattr(task.task.input, "role", None),
                strategy_instructions=task.config_snapshot.get(
                    "strategy_instructions"
                )
                or task.config_snapshot.get("strategyInstructions"),
            )
            action = action.model_copy(update={"message": decision.message})
            sanitized_result = result.model_copy(update={"action": action})
            message_replaced = decision.message_replaced
            policy_version = decision.policy_version

        # The repository receives only the sanitized result. The original
        # public message remains an in-memory local and is never used in an
        # exception or event payload.
        return await self._repository.submit_result(
            result=sanitized_result,
            server_clock=self._clock,
            message_replaced=message_replaced,
            public_output_policy_version=policy_version,
        )
