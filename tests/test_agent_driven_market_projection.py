"""PostgreSQL projection boundary for applied real-Agent market Results."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

from arena_agent_contracts import (
    ArenaDecideLimitsV1,
    ArenaInboundRfqV1,
    ArenaMarketDirectoryEntryV1,
    ArenaMarketIntentInputV1,
    ArenaMarketRfqInputV1,
    ArenaMarketSelectInputV1,
    ArenaReputationV1,
)
from arena_core.models import AppliedArenaAction
from arena_game.a2a_projection_worker import (
    AgentDrivenMarketProjectionWorker,
)
from arena_game.postgres import PostgresPawnhouseRepository
from tests.arena_core_helpers import NOW


DEADLINE = NOW + timedelta(seconds=30)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


class _ProjectionConnection:
    def __init__(
        self,
        authoritative_row,
        *,
        request_row=None,
        participant_busy=False,
    ):
        self.authoritative_row = authoritative_row
        self.request_row = request_row
        self.participant_busy = participant_busy
        self.calls: list[tuple[str, str, tuple[object, ...]]] = []

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, sql, *parameters):
        normalized = " ".join(sql.split())
        self.calls.append(("fetchrow", normalized, parameters))
        if "FROM public.arena_applied_agent_actions" in normalized:
            return self.authoritative_row
        if "INSERT INTO arena402.market_result_applications" in normalized:
            return {"result_id": self.authoritative_row["result_id"]}
        if "INSERT INTO arena402.market_projection_receipts" in normalized:
            return {"result_id": self.authoritative_row["result_id"]}
        if "INSERT INTO arena402.market_intents" in normalized:
            return {"intent_id": parameters[0]}
        if (
            "FROM arena402.market_intents" in normalized
            and "side = 'buy'" in normalized
        ):
            return {
                "intent_id": "buyer-intent-1",
                "game_participant_id": "buyer-game-agent",
                "good_id": "grain",
                "status": "open",
            }
        if (
            "FROM arena402.market_intents" in normalized
            and "side = 'sell'" in normalized
        ):
            return {
                "intent_id": parameters[0],
                "game_participant_id": f"seller:{parameters[0]}",
                "good_id": "grain",
                "status": "open",
            }
        if "FROM arena402.market_rfq_sessions" in normalized:
            return {
                "buyer_intent_id": "buyer-intent-1",
                "frozen_directory": [
                    {"intent_id": "seller-intent-1"},
                    {"intent_id": "seller-intent-2"},
                ],
                "attempt_count": 0,
                "max_attempts": 3,
                "status": "active",
            }
        if "INSERT INTO arena402.market_negotiation_requests" in normalized:
            return {"request_id": parameters[0]}
        if (
            "FROM arena402.market_negotiation_requests AS request"
            in normalized
            and "FOR UPDATE OF request" in normalized
        ):
            return {
                **(self.request_row or {}),
                "buyer_limit_price_atomic": 2_000_000,
                "seller_limit_price_atomic": 1_500_000,
                "buyer_cash_atomic": 20_000_000,
                "seller_inventory": 1,
            }
        if "FROM arena402.market_engagements" in normalized:
            return None
        if "INSERT INTO arena402.participant_round_slots" in normalized:
            return {"game_participant_id": parameters[2]}
        if (
            "UPDATE arena402.market_negotiation_requests" in normalized
            and "RETURNING request_id" in normalized
        ):
            return {"request_id": parameters[0]}
        raise AssertionError(f"unexpected fetchrow SQL: {normalized}")

    async def fetchval(self, sql, *parameters):
        normalized = " ".join(sql.split())
        self.calls.append(("fetchval", normalized, parameters))
        if "FROM arena402.market_negotiation_requests" in normalized:
            return False
        if "FROM arena402.participant_round_slots" in normalized:
            return self.participant_busy
        raise AssertionError(f"unexpected fetchval SQL: {normalized}")

    async def execute(self, sql, *parameters):
        normalized = " ".join(sql.split())
        self.calls.append(("execute", normalized, parameters))
        return "OK"


def _application(kind: str, result_id: str) -> AppliedArenaAction:
    return AppliedArenaAction(
        task_id=f"task:{kind}",
        result_id=result_id,
        kind=kind,  # type: ignore[arg-type]
        outcome="candidate",
        action={},
        entered_at=NOW,
        applied_at=NOW,
    )


def _row(kind: str, result_id: str, task_input, action):
    participant = (
        "seller-game-agent"
        if kind == "arena.market.select"
        else "buyer-game-agent"
    )
    return {
        "task_id": f"task:{kind}",
        "task_kind": kind,
        "game_id": "game-1",
        "round_id": "round-1",
        "game_agent_id": participant,
        "deadline_at": DEADLINE,
        "input_snapshot": task_input.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        ),
        "result_id": result_id,
        "application_outcome": "candidate",
        "applied_action": action,
        "authoritative_entered_at": NOW,
    }


def _intent_input() -> ArenaMarketIntentInputV1:
    return ArenaMarketIntentInputV1(
        phase="market_intent",
        game_id="game-1",
        round_id="round-1",
        round_index=1,
        cash="20.000000",
        holdings={"grain": 1},
        market={"grain": "2.000000"},
        reputation=ArenaReputationV1(failed_negotiations=0),
        limits=ArenaDecideLimitsV1(
            allowed_actions=["buy", "sell", "pass"],
            allowed_goods=["grain"],
        ),
        deadline_at=DEADLINE,
        market_expires_at=DEADLINE + timedelta(minutes=2),
    )


def _rfq_input() -> ArenaMarketRfqInputV1:
    return ArenaMarketRfqInputV1(
        phase="market_rfq",
        game_id="game-1",
        round_id="round-1",
        round_index=1,
        buyer_intent_id="buyer-intent-1",
        good="grain",
        public_price="1.800000",
        limit_price="2.000000",
        cash="20.000000",
        directory=[
            ArenaMarketDirectoryEntryV1(
                intent_id=f"seller-intent-{index}",
                agent_id=f"seller-agent-{index}",
                display_name=f"Seller {index}",
                good="grain",
                public_price="1.900000",
                expires_at=DEADLINE,
            )
            for index in (1, 2)
        ],
        deadline_at=DEADLINE,
    )


def _select_input() -> ArenaMarketSelectInputV1:
    return ArenaMarketSelectInputV1(
        phase="market_select",
        game_id="game-1",
        round_id="round-1",
        round_index=1,
        seller_intent_id="seller-intent-1",
        good="grain",
        public_price="1.900000",
        limit_price="1.600000",
        inventory_available=1,
        requests=[
            ArenaInboundRfqV1(
                request_id="request-1",
                buyer_agent_id="buyer-agent-1",
                buyer_display_name="Buyer",
                opening_price="1.700000",
                message="希望协商。",
                received_at=NOW,
            )
        ],
        deadline_at=DEADLINE,
    )


def test_intent_projection_persists_private_limit_but_never_emits_it() -> None:
    async def scenario() -> None:
        result_id = "runtime:" + ("1" * 64)
        action = {
            "action": "buy",
            "good": "grain",
            "publicPrice": "1.800000",
            "limitPrice": "2.000000",
            "message": "希望买入粮草。",
        }
        connection = _ProjectionConnection(
            _row(
                "arena.market.intent",
                result_id,
                _intent_input(),
                action,
            )
        )
        repository = PostgresPawnhouseRepository(
            "",
            pool=_Pool(connection),
        )

        receipt = await repository.project_agent_market_application(
            _application("arena.market.intent", result_id)
        )

        assert receipt["projected"] is True
        intent_insert = next(
            call
            for call in connection.calls
            if "INSERT INTO arena402.market_intents" in call[1]
        )
        assert intent_insert[2][8] == 2_000_000
        event = next(
            call
            for call in connection.calls
            if "INSERT INTO arena402.game_events" in call[1]
        )
        public_payload = json.loads(str(event[2][3]))
        assert public_payload["publicPriceAtomic"] == 1_800_000
        assert "limit" not in str(public_payload).lower()

    asyncio.run(scenario())


def test_one_rfq_result_projects_one_durable_attempt() -> None:
    async def scenario() -> None:
        result_id = "runtime:" + ("2" * 64)
        action = {
            "action": "request_negotiations",
            "requests": [
                {
                    "targetIntentId": "seller-intent-1",
                    "openingPrice": "1.700000",
                    "message": "请求协商",
                }
            ],
        }
        connection = _ProjectionConnection(
            _row("arena.market.rfq", result_id, _rfq_input(), action)
        )
        repository = PostgresPawnhouseRepository(
            "",
            pool=_Pool(connection),
        )

        receipt = await repository.project_agent_market_application(
            _application("arena.market.rfq", result_id)
        )

        assert receipt["requestIds"] == [
            "request:task:arena.market.rfq:1"
        ]
        request_inserts = [
            call
            for call in connection.calls
            if "INSERT INTO arena402.market_negotiation_requests" in call[1]
        ]
        assert len(request_inserts) == 1
        assert {call[2][8] for call in request_inserts} == {result_id}
        assert request_inserts[0][2][11] == 1
        session_update = next(
            call
            for call in connection.calls
            if "UPDATE arena402.market_rfq_sessions" in call[1]
        )
        assert session_update[2] == ("buyer-intent-1", 1)

    asyncio.run(scenario())


def test_seller_engage_projection_reserves_both_participant_slots() -> None:
    async def scenario() -> None:
        result_id = "runtime:" + ("3" * 64)
        request_row = {
            "request_id": "request-1",
            "status": "pending",
            "buyer_intent_id": "buyer-intent-1",
            "seller_intent_id": "seller-intent-1",
            "buyer_participant_id": "buyer-game-agent",
            "seller_participant_id": "seller-game-agent",
            "good_id": "grain",
            "opening_price_atomic": 1_800_000,
        }
        connection = _ProjectionConnection(
            _row(
                "arena.market.select",
                result_id,
                _select_input(),
                {"action": "engage", "requestId": "request-1"},
            ),
            request_row=request_row,
        )
        repository = PostgresPawnhouseRepository(
            "",
            pool=_Pool(connection),
        )

        receipt = await repository.project_agent_market_application(
            _application("arena.market.select", result_id)
        )

        assert receipt["engagementId"] == "engagement:request-1"
        slot_inserts = [
            call
            for call in connection.calls
            if "INSERT INTO arena402.participant_round_slots" in call[1]
        ]
        assert {call[2][2] for call in slot_inserts} == {
            "buyer-game-agent",
            "seller-game-agent",
        }
        claim = next(
            call
            for call in connection.calls
            if "INSERT INTO arena402.market_result_applications" in call[1]
        )
        assert claim[2][4:6] == (
            "engage",
            "engagement:request-1",
        )

    asyncio.run(scenario())


def test_busy_selection_closes_request_without_creating_engagement() -> None:
    async def scenario() -> None:
        result_id = "runtime:" + ("5" * 64)
        request_row = {
            "request_id": "request-1",
            "status": "pending",
            "buyer_intent_id": "buyer-intent-1",
            "seller_intent_id": "seller-intent-1",
            "buyer_participant_id": "buyer-game-agent",
            "seller_participant_id": "seller-game-agent",
            "good_id": "grain",
            "opening_price_atomic": 1_800_000,
        }
        connection = _ProjectionConnection(
            _row(
                "arena.market.select",
                result_id,
                _select_input(),
                {"action": "engage", "requestId": "request-1"},
            ),
            request_row=request_row,
            participant_busy=True,
        )
        repository = PostgresPawnhouseRepository(
            "",
            pool=_Pool(connection),
        )

        receipt = await repository.project_agent_market_application(
            _application("arena.market.select", result_id)
        )

        assert receipt["status"] == "counterparty_busy"
        assert not any(
            "INSERT INTO arena402.market_engagements" in call[1]
            for call in connection.calls
        )
        busy_update = next(
            call
            for call in connection.calls
            if "SET status = 'counterparty_busy'" in call[1]
        )
        assert busy_update[2][0] == "request-1"

    asyncio.run(scenario())


def test_projection_worker_retries_only_unreceipted_applied_results() -> None:
    class _Repository:
        def __init__(self):
            self.application = _application(
                "arena.market.intent",
                "runtime:" + ("4" * 64),
            )
            self.projected: list[str] = []

        async def pending_agent_market_applications(self, *, limit):
            assert limit == 10
            return [self.application]

        async def project_agent_market_application(self, application):
            self.projected.append(application.result_id)
            return {
                "resultId": application.result_id,
                "projected": True,
            }

    async def scenario() -> None:
        repository = _Repository()
        results = await AgentDrivenMarketProjectionWorker(
            repository
        ).run_once(limit=10)

        assert repository.projected == [repository.application.result_id]
        assert results[0]["projected"] is True

    asyncio.run(scenario())


def test_selection_timeout_expires_pending_requests_for_fallback() -> None:
    async def scenario() -> None:
        result_id = "runtime:" + ("6" * 64)
        authoritative = _row(
            "arena.market.select",
            result_id,
            _select_input(),
            None,
        )
        authoritative["application_outcome"] = "market_timeout"
        connection = _ProjectionConnection(authoritative)
        repository = PostgresPawnhouseRepository(
            "",
            pool=_Pool(connection),
        )

        receipt = await repository.project_agent_market_application(
            _application("arena.market.select", result_id)
        )

        assert receipt["outcome"] == "market_timeout"
        expired = next(
            call
            for call in connection.calls
            if "SET status = 'expired'" in call[1]
        )
        assert expired[2][0] == ["request-1"]

    asyncio.run(scenario())


def test_projection_authority_read_does_not_require_update_privilege() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "arena_game"
        / "postgres.py"
    ).read_text(encoding="utf-8")
    assert "FOR SHARE OF applied, task" not in source
