"""Durable Arena coordinator for task-driven Agent market execution."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from arena_agent_contracts import (
    ArenaCounterpartyQuoteV1,
    ArenaDecideInputV1,
    ArenaDecideLimitsV1,
    ArenaGoodRuleV1,
    ArenaMarketActivityV1,
    ArenaNegotiateInputV1,
    ArenaNegotiationMessageV1,
    ArenaPublicCounterpartyV1,
    ArenaPublicEventV1,
    ArenaReputationV1,
)
from arena_core.application_policy import derive_application
from arena_core.postgres_repository import PostgresArenaCoreRepository
from arena_core.task_factory import ArenaTaskFactory

from .goods import GOOD_IDS
from .postgres import PawnhouseRepositoryError, PostgresPawnhouseRepository


_LOGGER = logging.getLogger(__name__)
_GOLD_SCALE = 1_000_000


def _gold_decimal(atomic: int) -> str:
    if atomic < 0:
        raise ValueError("gold amount must be non-negative")
    whole, fraction = divmod(atomic, _GOLD_SCALE)
    return f"{whole}.{fraction:06d}"


def _public_events(
    values: list[dict[str, object]],
) -> list[ArenaPublicEventV1]:
    events: list[ArenaPublicEventV1] = []
    for value in values:
        payload = dict(value["payload"])
        events.append(
            ArenaPublicEventV1(
                event_id=str(value["event_id"]),
                event_type="world.event_revealed",
                occurred_at=value["occurred_at"],
                summary=str(
                    payload.get("narrative")
                    or payload.get("displayName")
                    or ""
                )[:500],
                payload=payload,
            )
        )
    return events


class PawnhouseAgentRuntimeCoordinator:
    """Coordinate Hosted and Connector tasks through one Arena Result Sink."""

    def __init__(
        self,
        *,
        pawnhouse: PostgresPawnhouseRepository,
        arena_core: PostgresArenaCoreRepository,
        worker_id: str | None = None,
        lease_seconds: int = 600,
    ) -> None:
        self._pawnhouse = pawnhouse
        self._arena_core = arena_core
        self._factory = ArenaTaskFactory(arena_core)
        self._worker_id = worker_id or (
            f"pawnhouse-coordinator-{uuid.uuid4().hex[:12]}"
        )
        self._lease_seconds = lease_seconds
        self._stopping = asyncio.Event()

    async def initialize(self) -> None:
        await self._arena_core.initialize()

    async def close(self) -> None:
        self.stop()
        await self._arena_core.close()

    def stop(self) -> None:
        self._stopping.set()

    async def run_forever(self, *, poll_seconds: float = 0.1) -> None:
        while not self._stopping.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.error(
                    "pawnhouse_agent_runtime_coordinator_cycle_failed"
                )
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=poll_seconds,
                    )
                except TimeoutError:
                    pass

    async def run_once(self) -> bool:
        claimed = await self._pawnhouse.claim_hosted_run(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if claimed is None:
            return False
        run_id = str(claimed["runtime_run_id"])
        try:
            await self._process(
                run_id=run_id,
                game_id=str(claimed["game_id"]),
                round_id=str(claimed["round_id"]),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.exception(
                "pawnhouse_hosted_run_failed run_id=%s stage_exception=%s",
                run_id,
                type(exc).__name__,
            )
            await self._pawnhouse.complete_hosted_run(
                runtime_run_id=run_id,
                worker_id=self._worker_id,
                error_code=(
                    f"runtime_{type(exc).__name__.lower()}"[:100]
                ),
            )
            return True
        await self._pawnhouse.complete_hosted_run(
            runtime_run_id=run_id,
            worker_id=self._worker_id,
        )
        return True

    async def _process(
        self,
        *,
        run_id: str,
        game_id: str,
        round_id: str,
    ) -> None:
        await self._pawnhouse.mark_hosted_run_running(
            runtime_run_id=run_id,
            worker_id=self._worker_id,
            stage="decide",
            lease_seconds=self._lease_seconds,
        )
        contexts = await self._pawnhouse.hosted_decide_contexts(
            game_id=game_id
        )
        task_pairs = []
        for context in contexts:
            view = self._decide_view(context)
            task = await self._factory.create_decide_task(
                game_agent_id=str(context["participant_id"]),
                participant_view=view,
                config_snapshot=dict(context["config_snapshot"]),
            )
            task_pairs.append((context, task))

        for context, task in task_pairs:
            result, action = await self._wait_and_consume(task)
            await self._pawnhouse.apply_hosted_decision(
                game_id=game_id,
                round_id=round_id,
                participant_id=str(context["participant_id"]),
                result_id=result.result.result_id,
                result_received_at=result.result_received_at,
                action=action or {"action": "pass"},
            )

        await self._pawnhouse.mark_hosted_run_running(
            runtime_run_id=run_id,
            worker_id=self._worker_id,
            stage="match",
            lease_seconds=self._lease_seconds,
        )
        await self._pawnhouse.pair_hosted_round(
            game_id=game_id,
            round_id=round_id,
        )

        await self._pawnhouse.mark_hosted_run_running(
            runtime_run_id=run_id,
            worker_id=self._worker_id,
            stage="negotiate",
            lease_seconds=self._lease_seconds,
        )
        negotiation_ids = (
            await self._pawnhouse.active_hosted_negotiation_ids(
                game_id=game_id,
                round_id=round_id,
            )
        )
        await asyncio.gather(
            *(
                self._run_negotiation(negotiation_id)
                for negotiation_id in negotiation_ids
            )
        )

    async def _run_negotiation(self, negotiation_id: str) -> None:
        while True:
            context = await self._pawnhouse.hosted_negotiation_context(
                negotiation_id=negotiation_id
            )
            if context is None:
                break
            task = await self._factory.create_negotiate_task(
                game_agent_id=str(context["participant_id"]),
                participant_view=self._negotiate_view(context),
                config_snapshot=dict(context["config_snapshot"]),
            )
            result, action = await self._wait_and_consume(task)
            await self._pawnhouse.apply_hosted_negotiation_action(
                negotiation_id=negotiation_id,
                result_id=result.result.result_id,
                action=action,
            )

    async def _wait_and_consume(
        self,
        task: Any,
    ) -> tuple[Any, dict[str, object] | None]:
        deadline = task.task.deadline_at
        while True:
            result = await self._arena_core.get_result_for_task(
                task.task.task_id
            )
            if result is not None:
                break
            if datetime.now(timezone.utc) >= deadline:
                await self._arena_core.finalize_expired(
                    server_clock=lambda: datetime.now(timezone.utc),
                    limit=50,
                )
            else:
                await asyncio.sleep(0.05)
                continue
            result = await self._arena_core.get_result_for_task(
                task.task.task_id
            )
            if result is None:
                raise PawnhouseRepositoryError(
                    "terminal_runtime_result_missing"
                )
            break

        application = derive_application(task.task, result.result)
        await self._arena_core.apply_result(
            result_id=result.result.result_id,
            server_clock=lambda: datetime.now(timezone.utc),
        )
        return result, application.action

    @staticmethod
    def _decide_view(context: dict[str, object]) -> ArenaDecideInputV1:
        return ArenaDecideInputV1(
            phase="decide",
            game_id=str(context["game_id"]),
            round_id=str(context["round_id"]),
            round_index=int(context["round_index"]),
            cash=_gold_decimal(int(context["cash_atomic"])),
            holdings=dict(context["holdings"]),
            market={
                str(good): _gold_decimal(int(price))
                for good, price in dict(context["market"]).items()
            },
            events=_public_events(list(context["events"])),
            reputation=ArenaReputationV1(failed_negotiations=0),
            limits=ArenaDecideLimitsV1(
                allowed_actions=["buy", "sell", "pass"],
                allowed_goods=list(GOOD_IDS),
            ),
            completed_actions=[],
            completed_trades=[],
            goods=[
                ArenaGoodRuleV1(
                    good=good_id,
                    fixed_quantity=1,
                    price_decimal_places=6,
                )
                for good_id in GOOD_IDS
            ],
            market_activity=[
                ArenaMarketActivityV1(
                    good=str(item["good"]),
                    last_clearing_price=(
                        None
                        if item.get("last_clearing_price_atomic") is None
                        else _gold_decimal(
                            int(item["last_clearing_price_atomic"])
                        )
                    ),
                    volume=int(item.get("volume", 0)),
                    buy_pressure_bps=int(item.get("buy_pressure_bps", 0)),
                    spread_bps=(
                        None
                        if item.get("spread_bps") is None
                        else int(item["spread_bps"])
                    ),
                )
                for item in list(context.get("market_activity", []))
            ],
            deadline_at=context["deadline_at"],
        )

    @staticmethod
    def _negotiate_view(
        context: dict[str, object],
    ) -> ArenaNegotiateInputV1:
        history = [
            ArenaNegotiationMessageV1(
                turn_sequence=int(item["turn_sequence"]),
                from_role=str(item["from_role"]),
                action=str(item["action"]),
                price=(
                    _gold_decimal(int(item["price_atomic"]))
                    if item["price_atomic"] is not None
                    else None
                ),
                message=item["message"],
            )
            for item in list(context["history"])
        ]
        latest = context["latest_quote"]
        latest_quote = (
            None
            if latest is None
            else ArenaCounterpartyQuoteV1(
                turn_sequence=int(latest["turn_sequence"]),
                from_role=str(latest["from_role"]),
                price=_gold_decimal(int(latest["price_atomic"])),
            )
        )
        return ArenaNegotiateInputV1(
            phase="negotiate",
            game_id=str(context["game_id"]),
            round_id=str(context["round_id"]),
            round_index=int(context["round_index"]),
            negotiation_id=str(context["negotiation_id"]),
            role=str(context["role"]),
            good=str(context["good"]),
            quantity=int(context["quantity"]),
            limit_price=(
                None
                if context.get("limit_price_atomic") is None
                else _gold_decimal(int(context["limit_price_atomic"]))
            ),
            cash=_gold_decimal(int(context["cash_atomic"])),
            inventory_available=int(context["inventory_available"]),
            counterparty=ArenaPublicCounterpartyV1(
                agent_id=str(context["counterparty_agent_id"]),
                display_name=str(context["counterparty_name"]),
                failed_negotiations=0,
            ),
            events=_public_events(list(context["events"])),
            history=history,
            latest_counterparty_quote=latest_quote,
            turn_sequence=int(context["turn_sequence"]),
            remaining_turns=int(context["remaining_turns"]),
            deadline_at=context["deadline_at"],
        )


# Compatibility for existing deployment entrypoints and imports.
PawnhouseHostedCoordinator = PawnhouseAgentRuntimeCoordinator


__all__ = [
    "PawnhouseAgentRuntimeCoordinator",
    "PawnhouseHostedCoordinator",
]
