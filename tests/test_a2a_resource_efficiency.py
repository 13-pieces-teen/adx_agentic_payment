from __future__ import annotations

import asyncio
import json
import weakref
from datetime import datetime, timezone
from types import SimpleNamespace

from arena_game.hosted_coordinator import PawnhouseAgentRuntimeCoordinator
from arena_game.postgres import PostgresPawnhouseRepository


class _Acquire:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self._connection)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _HostedContextConnection:
    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, query: str, *_: object):
        if "FROM arena402.rounds AS r" in query:
            return {
                "round_id": "round-1",
                "round_index": 1,
                "phase_deadline_at": datetime(
                    2030, 1, 1, tzinfo=timezone.utc
                ),
                "action_timeout_ms": 120_000,
                "round_count": 8,
            }
        if "market.liquidity_summarized" in query:
            return None
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query: str, *arguments: object):
        self.fetch_calls.append((query, arguments))
        if (
            "FROM arena402.game_participants AS p" in query
            and "JOIN public.game_agents AS ga" in query
        ):
            return [
                {
                    "game_participant_id": "participant-1",
                    "cash_atomic": 10_000_000,
                    "config_snapshot": {"provider": "fake"},
                    "config_hash": "sha256:" + "1" * 64,
                },
                {
                    "game_participant_id": "participant-2",
                    "cash_atomic": 10_000_000,
                    "config_snapshot": {"provider": "fake"},
                    "config_hash": "sha256:" + "2" * 64,
                },
            ]
        if "FROM arena402.holdings" in query:
            requested = arguments[0]
            participant_ids = (
                set(requested)
                if isinstance(requested, (list, tuple))
                else {requested}
            )
            rows = [
                {
                    "game_participant_id": "participant-1",
                    "good_id": "grain",
                    "quantity": 2,
                },
                {
                    "game_participant_id": "participant-2",
                    "good_id": "gems",
                    "quantity": 1,
                },
            ]
            return [
                row
                for row in rows
                if row["game_participant_id"] in participant_ids
            ]
        return []


def test_hosted_intent_context_reads_all_holdings_in_one_query() -> None:
    connection = _HostedContextConnection()
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_Pool(connection),
    )

    contexts = asyncio.run(
        repository.hosted_decide_contexts(game_id="game-1")
    )

    assert [context["holdings"] for context in contexts] == [
        {"grain": 2},
        {"gems": 1},
    ]
    holdings_queries = [
        query
        for query, _ in connection.fetch_calls
        if "FROM arena402.holdings" in query
    ]
    assert len(holdings_queries) == 1


class _SelectContextConnection:
    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, query: str, *_: object):
        if "FROM arena402.rounds AS round" in query:
            return {
                "round_index": 1,
                "phase_deadline_at": datetime(
                    2030, 1, 1, tzinfo=timezone.utc
                ),
                "action_timeout_ms": 120_000,
                "round_count": 8,
            }
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query: str, *arguments: object):
        self.fetch_calls.append((query, arguments))
        if (
            "FROM arena402.market_intents AS intent" in query
            and "request.status = 'pending'" in query
        ):
            return [
                {
                    "intent_id": "seller-intent-1",
                    "game_participant_id": "seller-1",
                    "good_id": "grain",
                    "quantity": 1,
                    "public_price_atomic": 1_900_000,
                    "limit_price_atomic": 1_800_000,
                    "inventory_available": 1,
                    "config_snapshot": {"provider": "fake"},
                },
                {
                    "intent_id": "seller-intent-2",
                    "game_participant_id": "seller-2",
                    "good_id": "gems",
                    "quantity": 1,
                    "public_price_atomic": 3_000_000,
                    "limit_price_atomic": 2_900_000,
                    "inventory_available": 1,
                    "config_snapshot": {"provider": "fake"},
                },
            ]
        if "FROM arena402.event_occurrences" in query:
            return []
        if "FROM arena402.market_negotiation_requests AS request" in query:
            requested = arguments[0]
            seller_intent_ids = (
                set(requested)
                if isinstance(requested, (list, tuple))
                else {requested}
            )
            rows = [
                {
                    "seller_intent_id": "seller-intent-1",
                    "request_id": "request-1",
                    "buyer_agent_id": "buyer-agent-1",
                    "buyer_display_name": "Buyer One",
                    "opening_price_atomic": 1_850_000,
                    "public_message": "grain request",
                    "created_at": datetime(
                        2026, 8, 8, 1, tzinfo=timezone.utc
                    ),
                },
                {
                    "seller_intent_id": "seller-intent-2",
                    "request_id": "request-2",
                    "buyer_agent_id": "buyer-agent-2",
                    "buyer_display_name": "Buyer Two",
                    "opening_price_atomic": 2_950_000,
                    "public_message": "gems request",
                    "created_at": datetime(
                        2026, 8, 8, 2, tzinfo=timezone.utc
                    ),
                },
            ]
            return [
                row
                for row in rows
                if row["seller_intent_id"] in seller_intent_ids
            ]
        raise AssertionError(f"Unexpected fetch query: {query}")


def test_market_select_context_reads_all_seller_inboxes_in_one_query() -> None:
    connection = _SelectContextConnection()
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_Pool(connection),
    )

    contexts = asyncio.run(
        repository.agent_market_select_contexts(
            game_id="game-1",
            round_id="round-1",
        )
    )

    assert [
        [request["request_id"] for request in context["requests"]]
        for context in contexts
    ] == [["request-1"], ["request-2"]]
    inbox_queries = [
        query
        for query, _ in connection.fetch_calls
        if "FROM arena402.market_negotiation_requests AS request" in query
        and "buyer_participant.agent_id" in query
    ]
    assert len(inbox_queries) == 1


class _RfqContextConnection:
    def __init__(self) -> None:
        self.deadline = datetime(2030, 1, 1, tzinfo=timezone.utc)

    async def fetchrow(self, query: str, *_: object):
        if "FROM arena402.rounds AS round" in query:
            return {
                "round_index": 1,
                "phase_deadline_at": self.deadline,
                "round_count": 8,
            }
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query: str, *_: object):
        if (
            "FROM arena402.market_intents AS intent" in query
            and "intent.side = 'buy'" in query
        ):
            return [
                {
                    "intent_id": "buyer-intent-1",
                    "game_participant_id": "buyer-1",
                    "good_id": "grain",
                    "quantity": 1,
                    "public_price_atomic": 1_800_000,
                    "limit_price_atomic": 2_000_000,
                    "expires_at": self.deadline,
                    "cash_atomic": 20_000_000,
                    "config_snapshot": {"provider": "fake-1"},
                },
                {
                    "intent_id": "buyer-intent-2",
                    "game_participant_id": "buyer-2",
                    "good_id": "gems",
                    "quantity": 1,
                    "public_price_atomic": 2_800_000,
                    "limit_price_atomic": 3_000_000,
                    "expires_at": self.deadline,
                    "cash_atomic": 20_000_000,
                    "config_snapshot": {"provider": "fake-2"},
                },
            ]
        if (
            "FROM arena402.market_intents AS intent" in query
            and "intent.side = 'sell'" in query
        ):
            return [
                {
                    "intent_id": "seller-intent-1",
                    "game_participant_id": "seller-1",
                    "agent_id": "seller-agent-1",
                    "display_name": "Seller One",
                    "good_id": "grain",
                    "quantity": 1,
                    "public_price_atomic": 1_900_000,
                    "limit_price_atomic": 1_900_000,
                    "failed_negotiations": 1,
                    "expires_at": self.deadline,
                },
                {
                    "intent_id": "seller-intent-2",
                    "game_participant_id": "seller-2",
                    "agent_id": "seller-agent-2",
                    "display_name": "Seller Two",
                    "good_id": "gems",
                    "quantity": 1,
                    "public_price_atomic": 2_900_000,
                    "limit_price_atomic": 2_900_000,
                    "failed_negotiations": 2,
                    "expires_at": self.deadline,
                },
            ]
        if "FROM arena402.event_occurrences" in query:
            return []
        raise AssertionError(f"Unexpected fetch query: {query}")


class _RfqContextPool(_Pool):
    def __init__(self, connection: _RfqContextConnection) -> None:
        super().__init__(connection)
        self.session_queries = 0
        self.prior_queries = 0

    @staticmethod
    def _session(
        buyer_intent_id: str,
        directory: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "buyer_intent_id": buyer_intent_id,
            "frozen_directory": directory,
            "attempt_count": 0,
            "max_attempts": 3,
            "status": "active",
            "deadline_at": datetime(2030, 1, 1, tzinfo=timezone.utc),
        }

    async def fetchrow(self, query: str, *arguments: object):
        if "INSERT INTO arena402.market_rfq_sessions" not in query:
            raise AssertionError(f"Unexpected pool fetchrow query: {query}")
        self.session_queries += 1
        directory = arguments[4]
        if isinstance(directory, str):
            directory = json.loads(directory)
        return self._session(str(arguments[0]), list(directory))

    async def fetch(self, query: str, *arguments: object):
        if "INSERT INTO arena402.market_rfq_sessions" in query:
            self.session_queries += 1
            buyer_intent_ids = list(arguments[0])
            directories = list(arguments[2])
            return [
                self._session(
                    str(buyer_intent_id),
                    list(
                        json.loads(directory)
                        if isinstance(directory, str)
                        else directory
                    ),
                )
                for buyer_intent_id, directory in zip(
                    buyer_intent_ids,
                    directories,
                    strict=True,
                )
            ]
        if "FROM arena402.market_negotiation_requests" in query:
            self.prior_queries += 1
            requested = arguments[0]
            buyer_intent_ids = (
                set(requested)
                if isinstance(requested, (list, tuple))
                else {str(requested)}
            )
            rows = [
                {
                    "buyer_intent_id": "buyer-intent-1",
                    "attempt_sequence": 1,
                    "seller_intent_id": "seller-intent-old",
                    "status": "expired",
                }
            ]
            return [
                row
                for row in rows
                if row["buyer_intent_id"] in buyer_intent_ids
            ]
        raise AssertionError(f"Unexpected pool fetch query: {query}")


def test_market_rfq_context_batches_sessions_and_prior_attempts() -> None:
    connection = _RfqContextConnection()
    pool = _RfqContextPool(connection)
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=pool,
    )

    contexts = asyncio.run(
        repository.agent_market_rfq_contexts(
            game_id="game-1",
            round_id="round-1",
        )
    )

    assert [context["buyer_intent_id"] for context in contexts] == [
        "buyer-intent-1",
        "buyer-intent-2",
    ]
    assert [
        [entry["intent_id"] for entry in context["directory"]]
        for context in contexts
    ] == [["seller-intent-1"], ["seller-intent-2"]]
    assert contexts[0]["prior_attempts"] == [
        {
            "attempt_sequence": 1,
            "target_intent_id": "seller-intent-old",
            "status": "timed_out",
        }
    ]
    assert pool.session_queries == 1
    assert pool.prior_queries == 1


class _TrackedTaskRecord:
    __slots__ = ("task", "payload", "__weakref__")

    def __init__(self) -> None:
        self.task = SimpleNamespace(
            task_id="task-1",
            deadline_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        self.payload = bytearray(1024 * 1024)


class _WaitCore:
    def __init__(self, task_ref: weakref.ReferenceType[object]) -> None:
        self._task_ref = task_ref
        self.applied: list[str] = []

    async def get_results_for_tasks(self, task_ids: list[str]):
        assert self._task_ref() is None
        return {
            "task-1": SimpleNamespace(
                result=SimpleNamespace(result_id="result-1")
            )
        }

    async def apply_result(self, *, result_id: str, **_: object) -> None:
        self.applied.append(result_id)

    async def finalize_expired(self, **_: object) -> None:
        raise AssertionError("future task must not be finalized")


class _WaitPawnhouse:
    def __init__(self) -> None:
        self.projected: list[str] = []

    async def renew_hosted_run_lease(self, **_: object) -> None:
        return None

    async def project_agent_market_result(self, *, result_id: str) -> None:
        self.projected.append(result_id)


def test_market_stage_wait_releases_full_task_snapshots_before_polling() -> None:
    async def scenario():
        tasks = [_TrackedTaskRecord()]
        task_ref = weakref.ref(tasks[0])
        core = _WaitCore(task_ref)
        pawnhouse = _WaitPawnhouse()
        coordinator = PawnhouseAgentRuntimeCoordinator(
            pawnhouse=pawnhouse,
            arena_core=core,
        )

        await coordinator._wait_apply_and_project_market_tasks(
            tasks,
            run_id="run-1",
            lease_epoch=1,
        )
        return tasks, core, pawnhouse

    tasks, core, pawnhouse = asyncio.run(scenario())

    assert tasks == []
    assert core.applied == ["result-1"]
    assert pawnhouse.projected == ["result-1"]


class _EngagementMaterializationConnection:
    def __init__(self) -> None:
        self.sequence_fetches = 0
        self.sequence_fetchvals = 0
        self.pairing_sequences: list[tuple[str, int]] = []
        self._legacy_next = {"grain": 11, "gems": 4}

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetch(self, query: str, *arguments: object):
        if "FROM arena402.market_engagements AS engagement" in query:
            created_at = datetime(2026, 8, 8, tzinfo=timezone.utc)
            return [
                {
                    "engagement_id": f"engagement-{index}",
                    "negotiation_id": f"negotiation-{index}",
                    "request_id": f"request-{index}",
                    "buyer_intent_id": f"buyer-intent-{index}",
                    "seller_intent_id": f"seller-intent-{index}",
                    "buyer_participant_id": f"buyer-{index}",
                    "seller_participant_id": f"seller-{index}",
                    "selection_result_id": f"select-result-{index}",
                    "good_id": good,
                    "buyer_limit_price_atomic": 2_000_000,
                    "seller_limit_price_atomic": 1_800_000,
                    "opening_price_atomic": 1_900_000,
                    "rfq_result_id": f"rfq-result-{index}",
                    "rfq_public_message": f"offer-{index}",
                    "created_at": created_at,
                }
                for index, good in enumerate(
                    ("grain", "grain", "gems"),
                    start=1,
                )
            ]
        if (
            "FROM arena402.pairings" in query
            and "GROUP BY good_id" in query
        ):
            self.sequence_fetches += 1
            assert arguments == (
                "round-1",
                ["gems", "grain"],
            )
            return [
                {"good_id": "grain", "max_sequence": 10},
                {"good_id": "gems", "max_sequence": 3},
            ]
        raise AssertionError(f"Unexpected fetch query: {query}")

    async def fetchval(self, query: str, *arguments: object):
        if "FROM arena402.pairings" not in query:
            raise AssertionError(f"Unexpected fetchval query: {query}")
        self.sequence_fetchvals += 1
        good = str(arguments[1])
        value = self._legacy_next[good]
        self._legacy_next[good] = value + 1
        return value

    async def execute(self, query: str, *arguments: object):
        if "INSERT INTO arena402.pairings" in query:
            self.pairing_sequences.append(
                (str(arguments[3]), int(arguments[8]))
            )
        return "INSERT 0 1"


def test_engagement_materialization_reads_pairing_sequences_once() -> None:
    connection = _EngagementMaterializationConnection()
    repository = PostgresPawnhouseRepository(
        "postgresql://unused",
        pool=_Pool(connection),
    )

    negotiation_ids = asyncio.run(
        repository.materialize_agent_market_engagements(
            game_id="game-1",
            round_id="round-1",
        )
    )

    assert negotiation_ids == [
        "negotiation-1",
        "negotiation-2",
        "negotiation-3",
    ]
    assert connection.sequence_fetches == 1
    assert connection.sequence_fetchvals == 0
    assert connection.pairing_sequences == [
        ("grain", 11),
        ("grain", 12),
        ("gems", 4),
    ]
