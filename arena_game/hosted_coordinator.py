"""Durable Arena coordinator for task-driven Agent market execution."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from arena_agent_contracts import (
    ArenaCompletedActionV1,
    ArenaCounterpartyQuoteV1,
    ArenaDecideInputV1,
    ArenaDecideLimitsV1,
    ArenaGoodRuleV1,
    ArenaMarketActivityV1,
    ArenaMarketDirectoryEntryV1,
    ArenaMarketIntentInputV1,
    ArenaMarketRfqInputV1,
    ArenaMarketSelectInputV1,
    ArenaInboundRfqV1,
    ArenaNegotiateInputV1,
    ArenaNegotiationMessageV1,
    ArenaPublicCounterpartyV1,
    ArenaPublicEventV1,
    ArenaReputationV1,
    ArenaRoundLiquidityV1,
    ArenaTradeSummaryV1,
)
from arena_core.application_policy import derive_application
from arena_core.postgres_repository import PostgresArenaCoreRepository
from arena_core.task_factory import ArenaTaskFactory

from .a2a_market import MARKET_AFTER_DECIDE_ACTION_SLOTS
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
        lease_epoch = int(claimed["lease_epoch"])
        try:
            await self._execute_claimed_run(
                run_id=run_id,
                game_id=str(claimed["game_id"]),
                round_id=str(claimed["round_id"]),
                lease_epoch=lease_epoch,
                market_protocol=str(
                    claimed.get("market_protocol") or "fcfs.v1"
                ),
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
                lease_epoch=lease_epoch,
                error_code=(
                    f"runtime_{type(exc).__name__.lower()}"[:100]
                ),
            )
            return True
        await self._pawnhouse.complete_hosted_run(
            runtime_run_id=run_id,
            worker_id=self._worker_id,
            lease_epoch=lease_epoch,
        )
        return True

    async def _execute_claimed_run(
        self,
        *,
        run_id: str,
        game_id: str,
        round_id: str,
        lease_epoch: int,
        market_protocol: str,
    ) -> None:
        process = asyncio.create_task(
            self._process(
                run_id=run_id,
                game_id=game_id,
                round_id=round_id,
                lease_epoch=lease_epoch,
                market_protocol=market_protocol,
            ),
            name=f"arena-runtime-run:{run_id}",
        )
        heartbeat = asyncio.create_task(
            self._renew_run_lease(
                run_id=run_id,
                lease_epoch=lease_epoch,
            ),
            name=f"arena-runtime-run-lease:{run_id}",
        )
        done, _ = await asyncio.wait(
            {process, heartbeat},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat in done:
            process.cancel()
            await asyncio.gather(process, return_exceptions=True)
            await heartbeat
            raise PawnhouseRepositoryError("runtime_run_lease_lost")
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        await process

    async def _renew_run_lease(
        self,
        *,
        run_id: str,
        lease_epoch: int,
    ) -> None:
        interval = max(1.0, self._lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            await self._pawnhouse.renew_hosted_run_lease(
                runtime_run_id=run_id,
                worker_id=self._worker_id,
                lease_epoch=lease_epoch,
                lease_seconds=self._lease_seconds,
            )

    async def _process(
        self,
        *,
        run_id: str,
        game_id: str,
        round_id: str,
        lease_epoch: int,
        market_protocol: str,
    ) -> None:
        if market_protocol == "agent_a2a.v1":
            await self._process_agent_market(
                run_id=run_id,
                game_id=game_id,
                round_id=round_id,
                lease_epoch=lease_epoch,
            )
            return
        if market_protocol != "fcfs.v1":
            raise PawnhouseRepositoryError(
                "runtime_run_market_protocol_invalid"
            )
        await self._pawnhouse.mark_hosted_run_running(
            runtime_run_id=run_id,
            worker_id=self._worker_id,
            lease_epoch=lease_epoch,
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

        # Poll all decide tasks as one set and start applying each terminal
        # result immediately. A slow Agent no longer sits at the head of a
        # sequential loop, and N participants no longer generate N polling
        # queries every 50 ms.
        contexts_by_task = {
            task.task.task_id: context for context, task in task_pairs
        }
        apply_jobs: list[asyncio.Task[None]] = []
        async for task, result in self._iter_terminal_results(
            [task for _, task in task_pairs]
        ):
            context = contexts_by_task[task.task.task_id]
            apply_jobs.append(
                asyncio.create_task(
                    self._consume_and_apply_decision(
                        run_id=run_id,
                        lease_epoch=lease_epoch,
                        game_id=game_id,
                        round_id=round_id,
                        context=context,
                        task=task,
                        result=result,
                    )
                )
            )
        if apply_jobs:
            await asyncio.gather(*apply_jobs)

        await self._pawnhouse.mark_hosted_run_running(
            runtime_run_id=run_id,
            worker_id=self._worker_id,
            lease_epoch=lease_epoch,
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
            lease_epoch=lease_epoch,
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
                self._run_negotiation(
                    negotiation_id,
                    run_id=run_id,
                    lease_epoch=lease_epoch,
                )
                for negotiation_id in negotiation_ids
            )
        )

    async def _process_agent_market(
        self,
        *,
        run_id: str,
        game_id: str,
        round_id: str,
        lease_epoch: int,
    ) -> None:
        """Resume the durable intent -> RFQ -> select -> negotiate chain."""

        while True:
            phase = await self._pawnhouse.agent_market_round_phase(
                game_id=game_id,
                round_id=round_id,
            )
            if phase == "decide":
                await self._pawnhouse.mark_hosted_run_running(
                    runtime_run_id=run_id,
                    worker_id=self._worker_id,
                    lease_epoch=lease_epoch,
                    stage="decide",
                    lease_seconds=self._lease_seconds,
                )
                contexts = await self._pawnhouse.hosted_decide_contexts(
                    game_id=game_id
                )
                tasks = [
                    await self._factory.create_market_intent_task(
                        game_agent_id=str(context["participant_id"]),
                        participant_view=self._market_intent_view(
                            context
                        ),
                        config_snapshot=dict(
                            context["config_snapshot"]
                        ),
                    )
                    for context in contexts
                ]
                await self._wait_apply_and_project_market_tasks(
                    tasks,
                    run_id=run_id,
                    lease_epoch=lease_epoch,
                )
                await self._pawnhouse.advance_agent_market_stage(
                    game_id=game_id,
                    round_id=round_id,
                    expected_phase="decide",
                    next_phase="match",
                    market_stage="rfq",
                )
                continue
            if phase == "match":
                await self._pawnhouse.mark_hosted_run_running(
                    runtime_run_id=run_id,
                    worker_id=self._worker_id,
                    lease_epoch=lease_epoch,
                    stage="match",
                    lease_seconds=self._lease_seconds,
                )
                contexts = (
                    await self._pawnhouse.agent_market_rfq_contexts(
                        game_id=game_id,
                        round_id=round_id,
                    )
                )
                tasks = [
                    await self._factory.create_market_rfq_task(
                        game_agent_id=str(context["participant_id"]),
                        participant_view=self._market_rfq_view(context),
                        config_snapshot=dict(
                            context["config_snapshot"]
                        ),
                    )
                    for context in contexts
                ]
                await self._wait_apply_and_project_market_tasks(
                    tasks,
                    run_id=run_id,
                    lease_epoch=lease_epoch,
                )
                await self._pawnhouse.advance_agent_market_stage(
                    game_id=game_id,
                    round_id=round_id,
                    expected_phase="match",
                    next_phase="negotiate",
                    market_stage="select",
                )
                continue
            if phase == "negotiate":
                await self._pawnhouse.mark_hosted_run_running(
                    runtime_run_id=run_id,
                    worker_id=self._worker_id,
                    lease_epoch=lease_epoch,
                    stage="negotiate",
                    lease_seconds=self._lease_seconds,
                )
                contexts = (
                    await self._pawnhouse.agent_market_select_contexts(
                        game_id=game_id,
                        round_id=round_id,
                    )
                )
                tasks = [
                    await self._factory.create_market_select_task(
                        game_agent_id=str(context["participant_id"]),
                        participant_view=self._market_select_view(
                            context
                        ),
                        config_snapshot=dict(
                            context["config_snapshot"]
                        ),
                    )
                    for context in contexts
                ]
                await self._wait_apply_and_project_market_tasks(
                    tasks,
                    run_id=run_id,
                    lease_epoch=lease_epoch,
                )
                await self._pawnhouse.materialize_agent_market_engagements(
                    game_id=game_id,
                    round_id=round_id,
                )
                negotiation_ids = (
                    await self._pawnhouse.active_hosted_negotiation_ids(
                        game_id=game_id,
                        round_id=round_id,
                    )
                )
                await asyncio.gather(
                    *(
                        self._run_negotiation(
                            negotiation_id,
                            run_id=run_id,
                            lease_epoch=lease_epoch,
                        )
                        for negotiation_id in negotiation_ids
                    )
                )
                fallback_contexts = (
                    await self._pawnhouse.agent_market_fallback_rfq_contexts(
                        game_id=game_id,
                        round_id=round_id,
                    )
                )
                if fallback_contexts:
                    fallback_tasks = [
                        await self._factory.create_market_rfq_task(
                            game_agent_id=str(
                                context["participant_id"]
                            ),
                            participant_view=self._market_rfq_view(
                                context
                            ),
                            config_snapshot=dict(
                                context["config_snapshot"]
                            ),
                        )
                        for context in fallback_contexts
                    ]
                    await self._wait_apply_and_project_market_tasks(
                        fallback_tasks,
                        run_id=run_id,
                        lease_epoch=lease_epoch,
                    )
                    continue
                return
            raise PawnhouseRepositoryError(
                "agent_market_round_phase_invalid"
            )

    async def _wait_apply_and_project_market_tasks(
        self,
        tasks: list[Any],
        *,
        run_id: str,
        lease_epoch: int,
    ) -> None:
        async for _, result in self._iter_terminal_results(tasks):
            await self._pawnhouse.renew_hosted_run_lease(
                runtime_run_id=run_id,
                worker_id=self._worker_id,
                lease_epoch=lease_epoch,
                lease_seconds=self._lease_seconds,
            )
            await self._arena_core.apply_result(
                result_id=result.result.result_id,
                server_clock=lambda: datetime.now(timezone.utc),
            )
            await self._pawnhouse.project_agent_market_result(
                result_id=result.result.result_id,
            )

    async def _run_negotiation(
        self,
        negotiation_id: str,
        *,
        run_id: str,
        lease_epoch: int,
    ) -> None:
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
            result, action = await self._wait_and_consume(
                task,
                run_id=run_id,
                lease_epoch=lease_epoch,
            )
            await self._pawnhouse.apply_hosted_negotiation_action(
                negotiation_id=negotiation_id,
                result_id=result.result.result_id,
                action=action,
            )

    async def _wait_and_consume(
        self,
        task: Any,
        *,
        run_id: str,
        lease_epoch: int,
    ) -> tuple[Any, dict[str, object] | None]:
        result = None
        async for _, terminal_result in self._iter_terminal_results([task]):
            result = terminal_result
        if result is None:
            raise PawnhouseRepositoryError("terminal_runtime_result_missing")
        await self._pawnhouse.renew_hosted_run_lease(
            runtime_run_id=run_id,
            worker_id=self._worker_id,
            lease_epoch=lease_epoch,
            lease_seconds=self._lease_seconds,
        )
        application = derive_application(task.task, result.result)
        await self._arena_core.apply_result(
            result_id=result.result.result_id,
            server_clock=lambda: datetime.now(timezone.utc),
        )
        return result, application.action

    async def _iter_terminal_results(self, tasks: list[Any]):
        """Yield terminal task results without per-task polling.

        The Result Sink timestamp remains authoritative for FCFS ordering; the
        generator only changes when independent results become available to
        the idempotent Arena applicator.
        """
        pending = {task.task.task_id: task for task in tasks}
        while pending:
            results = await self._arena_core.get_results_for_tasks(
                list(pending)
            )
            for task_id, result in results.items():
                task = pending.pop(task_id, None)
                if task is not None:
                    yield task, result
            if not pending:
                break

            now = datetime.now(timezone.utc)
            expired = [
                task
                for task in pending.values()
                if now >= task.task.deadline_at
            ]
            if expired:
                await self._arena_core.finalize_expired(
                    server_clock=lambda: datetime.now(timezone.utc),
                    limit=max(50, len(expired)),
                )
                finalized = await self._arena_core.get_results_for_tasks(
                    [task.task.task_id for task in expired]
                )
                missing = [
                    task.task.task_id
                    for task in expired
                    if task.task.task_id not in finalized
                ]
                if missing:
                    raise PawnhouseRepositoryError(
                        "terminal_runtime_result_missing"
                    )
                continue
            await asyncio.sleep(0.05)

    async def _consume_and_apply_decision(
        self,
        *,
        run_id: str,
        lease_epoch: int,
        game_id: str,
        round_id: str,
        context: dict[str, object],
        task: Any,
        result: Any,
    ) -> None:
        # Fence the business write itself, not only the stage transition. A
        # coordinator resumed after a long process pause must fail before it
        # can apply a stale result.
        await self._pawnhouse.renew_hosted_run_lease(
            runtime_run_id=run_id,
            worker_id=self._worker_id,
            lease_epoch=lease_epoch,
            lease_seconds=self._lease_seconds,
        )
        application = derive_application(task.task, result.result)
        await self._arena_core.apply_result(
            result_id=result.result.result_id,
            server_clock=lambda: datetime.now(timezone.utc),
        )
        await self._pawnhouse.apply_hosted_decision(
            game_id=game_id,
            round_id=round_id,
            participant_id=str(context["participant_id"]),
            result_id=result.result.result_id,
            result_received_at=result.result_received_at,
            action=application.action or {"action": "pass"},
        )

    @staticmethod
    def _decide_view(context: dict[str, object]) -> ArenaDecideInputV1:
        cash_atomic = int(context["cash_atomic"])
        holdings = {
            str(good): int(quantity)
            for good, quantity in dict(context["holdings"]).items()
        }
        can_buy = cash_atomic > 0
        sellable_goods = [
            good_id
            for good_id in GOOD_IDS
            if holdings.get(good_id, 0) > 0
        ]
        allowed_actions = [
            *([] if not can_buy else ["buy"]),
            *([] if not sellable_goods else ["sell"]),
            "pass",
        ]
        return ArenaDecideInputV1(
            phase="decide",
            game_id=str(context["game_id"]),
            round_id=str(context["round_id"]),
            round_index=int(context["round_index"]),
            round_count=int(context["round_count"]),
            rounds_remaining=int(context["rounds_remaining"]),
            cash=_gold_decimal(cash_atomic),
            holdings=holdings,
            market={
                str(good): _gold_decimal(int(price))
                for good, price in dict(context["market"]).items()
            },
            event_implied_final={
                str(good): _gold_decimal(int(price))
                for good, price in dict(
                    context.get("event_implied_final", {})
                ).items()
            },
            events=_public_events(list(context["events"])),
            reputation=ArenaReputationV1(
                failed_negotiations=int(
                    context.get("failed_negotiations", 0)
                )
            ),
            limits=ArenaDecideLimitsV1(
                allowed_actions=allowed_actions,
                allowed_goods=(
                    list(GOOD_IDS) if can_buy else sellable_goods
                ),
            ),
            completed_actions=[
                ArenaCompletedActionV1(
                    round_id=str(item["round_id"]),
                    action=dict(item["action"]),
                )
                for item in list(context.get("completed_actions", []))
            ],
            completed_trades=[
                ArenaTradeSummaryV1(
                    round_id=str(item["round_id"]),
                    negotiation_id=str(item["negotiation_id"]),
                    role=str(item["role"]),
                    good=str(item["good"]),
                    quantity=int(item["quantity"]),
                    price=_gold_decimal(int(item["price_atomic"])),
                )
                for item in list(context.get("completed_trades", []))
            ],
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
            previous_round_liquidity=(
                None
                if context.get("previous_round_liquidity") is None
                else ArenaRoundLiquidityV1.model_validate(
                    context["previous_round_liquidity"]
                )
            ),
            deadline_at=context["deadline_at"],
        )

    @classmethod
    def _market_intent_view(
        cls,
        context: dict[str, object],
    ) -> ArenaMarketIntentInputV1:
        payload = cls._decide_view(context).model_dump(
            mode="python",
            by_alias=False,
        )
        payload["phase"] = "market_intent"
        payload["market_protocol"] = "agent_a2a.v1"
        payload["market_expires_at"] = (
            context["deadline_at"]
            + timedelta(
                milliseconds=(
                    MARKET_AFTER_DECIDE_ACTION_SLOTS
                    * int(context["action_timeout_ms"])
                )
            )
        )
        return ArenaMarketIntentInputV1.model_validate(payload)

    @staticmethod
    def _market_rfq_view(
        context: dict[str, object],
    ) -> ArenaMarketRfqInputV1:
        return ArenaMarketRfqInputV1(
            phase="market_rfq",
            market_protocol="agent_a2a.v1",
            game_id=str(context["game_id"]),
            round_id=str(context["round_id"]),
            round_index=int(context["round_index"]),
            round_count=int(context["round_count"]),
            rounds_remaining=int(context["rounds_remaining"]),
            buyer_intent_id=str(context["buyer_intent_id"]),
            good=str(context["good"]),
            quantity=int(context["quantity"]),
            public_price=_gold_decimal(
                int(context["public_price_atomic"])
            ),
            limit_price=_gold_decimal(
                int(context["limit_price_atomic"])
            ),
            cash=_gold_decimal(int(context["cash_atomic"])),
            directory=[
                ArenaMarketDirectoryEntryV1(
                    intent_id=str(entry["intent_id"]),
                    agent_id=str(entry["agent_id"]),
                    display_name=str(entry["display_name"]),
                    good=str(entry["good"]),
                    quantity=int(entry["quantity"]),
                    public_price=_gold_decimal(
                        int(entry["public_price_atomic"])
                    ),
                    failed_negotiations=int(
                        entry.get("failed_negotiations", 0)
                    ),
                    expires_at=entry["expires_at"],
                )
                for entry in list(context["directory"])
            ],
            max_outbound_rfq=3,
            attempt_sequence=int(
                context.get("attempt_sequence", 1)
            ),
            remaining_rfq_attempts=int(
                context.get("remaining_rfq_attempts", 3)
            ),
            prior_attempts=list(context.get("prior_attempts", [])),
            events=_public_events(list(context["events"])),
            deadline_at=context["deadline_at"],
        )

    @staticmethod
    def _market_select_view(
        context: dict[str, object],
    ) -> ArenaMarketSelectInputV1:
        requests = list(context["requests"])
        return ArenaMarketSelectInputV1(
            phase="market_select",
            market_protocol="agent_a2a.v1",
            game_id=str(context["game_id"]),
            round_id=str(context["round_id"]),
            round_index=int(context["round_index"]),
            round_count=int(context["round_count"]),
            rounds_remaining=int(context["rounds_remaining"]),
            seller_intent_id=str(context["seller_intent_id"]),
            good=str(context["good"]),
            quantity=int(context["quantity"]),
            public_price=_gold_decimal(
                int(context["public_price_atomic"])
            ),
            limit_price=_gold_decimal(
                int(context["limit_price_atomic"])
            ),
            inventory_available=int(
                context["inventory_available"]
            ),
            requests=[
                ArenaInboundRfqV1(
                    request_id=str(request["request_id"]),
                    buyer_agent_id=str(request["buyer_agent_id"]),
                    buyer_display_name=str(
                        request["buyer_display_name"]
                    ),
                    opening_price=_gold_decimal(
                        int(request["opening_price_atomic"])
                    ),
                    message=str(request["message"]),
                    received_at=request["received_at"],
                )
                for request in requests
            ],
            max_engagements=1,
            events=_public_events(list(context["events"])),
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
            round_count=int(context["round_count"]),
            rounds_remaining=int(context["rounds_remaining"]),
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
                failed_negotiations=int(
                    context.get("counterparty_failed_negotiations", 0)
                ),
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
