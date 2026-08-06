"""Create immutable, idempotent Arena Agent tasks from authoritative snapshots."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from arena_agent_contracts import (
    AGENT_TASK_SCHEMA_VERSION_V1,
    ArenaAgentTaskV1,
    ArenaDecideInputV1,
    ArenaMarketIntentInputV1,
    ArenaMarketRfqInputV1,
    ArenaMarketSelectInputV1,
    ArenaNegotiateInputV1,
    ArenaTaskKindV1,
    market_select_request_set_token,
)

from .hashing import sha256_identifier
from .ingress_security import secure_config_snapshot
from .models import ArenaTaskRecord
from .repository import (
    ArenaCoreRepository,
    ArenaIdempotencyConflictError,
)


class ArenaTaskFactory:
    def __init__(
        self,
        repository: ArenaCoreRepository,
        *,
        task_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._task_id_factory = task_id_factory or (
            lambda: f"task_{uuid.uuid4().hex}"
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def create_decide_task(
        self,
        *,
        game_agent_id: str,
        participant_view: ArenaDecideInputV1,
        config_snapshot: dict[str, Any],
    ) -> ArenaTaskRecord:
        idempotency_key = (
            f"{participant_view.game_id}:{participant_view.round_id}:"
            f"{game_agent_id}:decide"
        )
        return await self._create(
            kind="arena.decide",
            game_agent_id=game_agent_id,
            participant_view=participant_view,
            config_snapshot=config_snapshot,
            negotiation_id=None,
            idempotency_key=idempotency_key,
        )

    async def create_negotiate_task(
        self,
        *,
        game_agent_id: str,
        participant_view: ArenaNegotiateInputV1,
        config_snapshot: dict[str, Any],
    ) -> ArenaTaskRecord:
        idempotency_key = (
            f"{participant_view.game_id}:{participant_view.round_id}:"
            f"{participant_view.negotiation_id}:{participant_view.turn_sequence}:"
            f"{game_agent_id}:negotiate"
        )
        return await self._create(
            kind="arena.negotiate",
            game_agent_id=game_agent_id,
            participant_view=participant_view,
            config_snapshot=config_snapshot,
            negotiation_id=participant_view.negotiation_id,
            idempotency_key=idempotency_key,
        )

    async def create_market_intent_task(
        self,
        *,
        game_agent_id: str,
        participant_view: ArenaMarketIntentInputV1,
        config_snapshot: dict[str, Any],
    ) -> ArenaTaskRecord:
        return await self._create_market_task(
            kind="arena.market.intent",
            game_agent_id=game_agent_id,
            participant_view=participant_view,
            config_snapshot=config_snapshot,
        )

    async def create_market_rfq_task(
        self,
        *,
        game_agent_id: str,
        participant_view: ArenaMarketRfqInputV1,
        config_snapshot: dict[str, Any],
    ) -> ArenaTaskRecord:
        return await self._create_market_task(
            kind="arena.market.rfq",
            game_agent_id=game_agent_id,
            participant_view=participant_view,
            config_snapshot=config_snapshot,
        )

    async def create_market_select_task(
        self,
        *,
        game_agent_id: str,
        participant_view: ArenaMarketSelectInputV1,
        config_snapshot: dict[str, Any],
    ) -> ArenaTaskRecord:
        return await self._create_market_task(
            kind="arena.market.select",
            game_agent_id=game_agent_id,
            participant_view=participant_view,
            config_snapshot=config_snapshot,
        )

    async def _create_market_task(
        self,
        *,
        kind: ArenaTaskKindV1,
        game_agent_id: str,
        participant_view: (
            ArenaMarketIntentInputV1
            | ArenaMarketRfqInputV1
            | ArenaMarketSelectInputV1
        ),
        config_snapshot: dict[str, Any],
    ) -> ArenaTaskRecord:
        suffix = kind.removeprefix("arena.market.")
        task_suffix = suffix
        if kind == "arena.market.rfq":
            assert isinstance(participant_view, ArenaMarketRfqInputV1)
            task_suffix = (
                f"{suffix}:{participant_view.attempt_sequence}"
            )
        elif kind == "arena.market.select":
            assert isinstance(participant_view, ArenaMarketSelectInputV1)
            request_set_token = market_select_request_set_token(
                [
                    request.request_id
                    for request in participant_view.requests
                ]
            )
            task_suffix = f"{suffix}:{request_set_token}"
        return await self._create(
            kind=kind,
            game_agent_id=game_agent_id,
            participant_view=participant_view,
            config_snapshot=config_snapshot,
            negotiation_id=None,
            idempotency_key=(
                f"{participant_view.game_id}:{participant_view.round_id}:"
                f"{game_agent_id}:market-{task_suffix}"
            ),
        )

    async def _create(
        self,
        *,
        kind: ArenaTaskKindV1,
        game_agent_id: str,
        participant_view: (
            ArenaDecideInputV1
            | ArenaNegotiateInputV1
            | ArenaMarketIntentInputV1
            | ArenaMarketRfqInputV1
            | ArenaMarketSelectInputV1
        ),
        config_snapshot: dict[str, Any],
        negotiation_id: str | None,
        idempotency_key: str,
    ) -> ArenaTaskRecord:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Arena Task Factory clock must return an aware datetime")

        participant_snapshot = participant_view.model_copy(deep=True)
        # Public Arena snapshots are also a durable boundary. Re-scan every
        # nested public value so an upstream projection bug cannot persist a
        # credential, direct contact detail, or invisible control character.
        secure_config_snapshot(
            participant_snapshot.model_dump(
                mode="python",
                by_alias=True,
                exclude_none=False,
            )
        )
        config_snapshot_copy = secure_config_snapshot(config_snapshot)
        input_hash = sha256_identifier(participant_snapshot)
        config_hash = sha256_identifier(config_snapshot_copy)
        if participant_snapshot.deadline_at <= now:
            existing = await self._repository.get_task_by_idempotency(
                game_agent_id=game_agent_id,
                idempotency_key=idempotency_key,
            )
            if existing is None:
                raise ValueError("Arena task deadline must be in the future")
            if (
                existing.task.kind != kind
                or existing.task.input_hash != input_hash
                or existing.config_hash != config_hash
            ):
                raise ArenaIdempotencyConflictError(
                    "Elapsed Arena task recovery conflicts with its "
                    "frozen snapshot"
                )
            return existing

        task = ArenaAgentTaskV1(
            task_id=self._task_id_factory(),
            kind=kind,
            schema_version=AGENT_TASK_SCHEMA_VERSION_V1,
            game_id=participant_snapshot.game_id,
            round_id=participant_snapshot.round_id,
            game_agent_id=game_agent_id,
            negotiation_id=negotiation_id,
            deadline_at=participant_snapshot.deadline_at,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            input=participant_snapshot,
        )
        return await self._repository.create_task(
            task=task,
            config_snapshot=config_snapshot_copy,
            config_hash=config_hash,
            created_at=now,
        )
