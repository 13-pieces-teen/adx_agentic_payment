import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from arena_agent_contracts import (
    AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
    AGENT_TASK_SCHEMA_VERSION_V1,
    AgentTaskResultV1,
    ArenaAgentTaskV1,
    ArenaPublicEventV1,
    BuyAction,
    ProposeAction,
)
from arena_core.hashing import sha256_identifier, sha256_text_identifier
from arena_core.ingress_security import ArenaIngressSecurityError
from arena_core.models import SubmissionDisposition, TaskStatus
from arena_core.postgres_repository import PostgresArenaCoreRepository
from tests.arena_core_helpers import NOW, decide_input, negotiate_input


DB_TIME = datetime(2026, 7, 24, 12, 0, 7, tzinfo=timezone.utc)


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


class ScriptedConnection:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def transaction(self):
        return _Transaction()

    async def _call(self, method, sql, args):
        normalized = " ".join(sql.split())
        self.calls.append((method, normalized, args))
        return self.handler(method, normalized, args)

    async def execute(self, sql, *args):
        return await self._call("execute", sql, args)

    async def fetchrow(self, sql, *args):
        return await self._call("fetchrow", sql, args)

    async def fetch(self, sql, *args):
        return await self._call("fetch", sql, args)

    async def fetchval(self, sql, *args):
        return await self._call("fetchval", sql, args)


class FakePool:
    def __init__(self, connection):
        self.connection = connection
        self.closed = False

    def acquire(self):
        return _Acquire(self.connection)

    async def execute(self, sql, *args):
        return await self.connection.execute(sql, *args)

    async def fetchrow(self, sql, *args):
        return await self.connection.fetchrow(sql, *args)

    async def fetch(self, sql, *args):
        return await self.connection.fetch(sql, *args)

    async def fetchval(self, sql, *args):
        return await self.connection.fetchval(sql, *args)

    async def close(self):
        self.closed = True


def _task(*, hosted=False, negotiate=False):
    participant_view = negotiate_input() if negotiate else decide_input()
    game_agent_id = "game-agent-1"
    kind = "arena.negotiate" if negotiate else "arena.decide"
    negotiation_id = (
        participant_view.negotiation_id if negotiate else None
    )
    if negotiate:
        idempotency_key = (
            f"{participant_view.game_id}:{participant_view.round_id}:"
            f"{participant_view.negotiation_id}:"
            f"{participant_view.turn_sequence}:{game_agent_id}:negotiate"
        )
    else:
        idempotency_key = (
            f"{participant_view.game_id}:{participant_view.round_id}:"
            f"{game_agent_id}:decide"
        )
    task = ArenaAgentTaskV1(
        task_id="task-1",
        kind=kind,
        schema_version=AGENT_TASK_SCHEMA_VERSION_V1,
        game_id=participant_view.game_id,
        round_id=participant_view.round_id,
        game_agent_id=game_agent_id,
        negotiation_id=negotiation_id,
        deadline_at=participant_view.deadline_at,
        idempotency_key=idempotency_key,
        input_hash=sha256_identifier(participant_view),
        input=participant_view,
    )
    config = {
        "provider": "fake",
        "model": "fake-structured-v1",
    }
    if hosted:
        config["credential_id"] = "credential-1"
    return task, config


def _task_row(task, config, **overrides):
    row = {
        "task_id": task.task_id,
        "task_kind": task.kind,
        "schema_version": task.schema_version,
        "game_id": task.game_id,
        "round_id": task.round_id,
        "game_agent_id": task.game_agent_id,
        "negotiation_id": task.negotiation_id,
        "deadline_at": task.deadline_at,
        "idempotency_key": task.idempotency_key,
        "input_snapshot": task.input.model_dump(
            mode="json", by_alias=True, exclude_none=False
        ),
        "input_hash": task.input_hash,
        "runtime_config_snapshot": config,
        "config_hash": sha256_identifier(config),
        "status": "queued",
        "attempt_count": 0,
        "leased_by": None,
        "lease_expires_at": None,
        "created_at": DB_TIME,
        "completed_at": None,
    }
    row.update(overrides)
    return row


def _result_row(result, *, result_id=None, **overrides):
    internal_id = result_id or result.result_id
    row = {
        "result_id": internal_id,
        "task_id": result.task_id,
        "result_schema_version": result.schema_version,
        "result_hash": sha256_identifier(result),
        "runtime_status": result.status,
        "candidate_action": (
            None
            if result.action is None
            else result.action.model_dump(mode="json", by_alias=True)
        ),
        "message_replaced": False,
        "public_output_policy_version": None,
        "result_received_at": DB_TIME,
        "apply_status": "pending",
        "arena_applied_at": None,
        "arena_rejected_at": None,
        "error_class": None,
    }
    row.update(overrides)
    return row


def _must_not_run():
    raise AssertionError("server_clock must be ignored by PostgreSQL repository")


def test_create_task_uses_frozen_game_agent_config_and_database_created_at():
    async def scenario():
        task, config = _task(hosted=True)
        config_hash = sha256_identifier(config)
        returned_task_row = _task_row(task, config)

        def handler(method, sql, args):
            if method == "fetchrow" and "FROM game_agents AS ga" in sql:
                return {
                    "runtime_binding_id": "binding-1",
                    "config_snapshot": json.dumps(config),
                    "config_hash": config_hash,
                    "runtime_kind": "hosted",
                    "credential_id": "credential-1",
                }
            if method == "fetchrow" and "INSERT INTO arena_agent_tasks" in sql:
                return returned_task_row
            if method == "execute" and "arena_agent_task_events" in sql:
                return "INSERT 0 1"
            raise AssertionError((method, sql, args))

        connection = ScriptedConnection(handler)
        repository = PostgresArenaCoreRepository(
            "postgresql://unused",
            pool=FakePool(connection),
        )
        created = await repository.create_task(
            task=task,
            config_snapshot=config,
            config_hash=config_hash,
            created_at=NOW,
        )

        assert created.created_at == DB_TIME
        assert created.config_snapshot == config
        insert_call = next(
            call
            for call in connection.calls
            if "INSERT INTO arena_agent_tasks" in call[1]
        )
        assert insert_call[2][6] == "binding-1"
        assert insert_call[2][7] == "credential-1"
        assert insert_call[2][12] == task.input.model_dump(
            mode="json", by_alias=True, exclude_none=False
        )
        assert insert_call[2][14] == config

    asyncio.run(scenario())


def test_connector_task_claim_freezes_route_and_can_be_deferred():
    async def scenario():
        task, config = _task()
        leased = _task_row(
            task,
            config,
            status="leased",
            leased_by="connector-dispatcher-1",
            lease_expires_at=DB_TIME + timedelta(seconds=5),
        )
        leased.update(
            {
                "connector_binding_id": "binding-local-1",
                "connector_binding_epoch": 9,
            }
        )

        def handler(method, sql, args):
            if method == "fetch" and "connector_binding_id" in sql:
                assert args == ("connector-dispatcher-1", 25, 5)
                return [leased]
            if method == "fetchval" and "SET lease_expires_at" in sql:
                assert args == (
                    task.task_id,
                    "connector-dispatcher-1",
                    1,
                )
                return True
            raise AssertionError((method, sql, args))

        repository = PostgresArenaCoreRepository(
            "postgresql://unused",
            pool=FakePool(ScriptedConnection(handler)),
        )
        claims = await repository.claim_connector_tasks(
            worker_id="connector-dispatcher-1",
            limit=25,
            lease_seconds=5,
        )

        assert len(claims) == 1
        assert claims[0].task == task
        assert claims[0].connector_binding_id == "binding-local-1"
        assert claims[0].connector_binding_epoch == 9
        await repository.defer_connector_task(
            task_id=task.task_id,
            worker_id="connector-dispatcher-1",
            delay_seconds=1,
        )

    asyncio.run(scenario())


def test_create_task_rejects_raw_config_secret_before_sql():
    async def scenario():
        connection = ScriptedConnection(
            lambda method, sql, args: (_ for _ in ()).throw(
                AssertionError("SQL must not run")
            )
        )
        repository = PostgresArenaCoreRepository(
            "postgresql://unused",
            pool=FakePool(connection),
        )
        task, _ = _task()
        raw_secret = "sk-abcdefghijklmnopqrstuvwxyz"
        config = {"api_key": raw_secret}

        with pytest.raises(ArenaIngressSecurityError) as captured:
            await repository.create_task(
                task=task,
                config_snapshot=config,
                config_hash=sha256_identifier(config),
                created_at=NOW,
            )

        assert raw_secret not in repr(captured.value)
        assert connection.calls == []

    asyncio.run(scenario())


def test_create_task_rejects_secret_in_public_snapshot_before_sql():
    async def scenario():
        connection = ScriptedConnection(
            lambda method, sql, args: (_ for _ in ()).throw(
                AssertionError("SQL must not run")
            )
        )
        repository = PostgresArenaCoreRepository(
            "postgresql://unused",
            pool=FakePool(connection),
        )
        raw_secret = "sk-abcdefghijklmnopqrstuvwxyz"
        participant_view = decide_input()
        participant_view.events.append(
            ArenaPublicEventV1(
                event_id="event-secret",
                event_type="notice",
                occurred_at=NOW,
                summary=f"unexpected credential {raw_secret}",
            )
        )
        task = ArenaAgentTaskV1(
            task_id="task-secret-input",
            kind="arena.decide",
            schema_version=AGENT_TASK_SCHEMA_VERSION_V1,
            game_id=participant_view.game_id,
            round_id=participant_view.round_id,
            game_agent_id="game-agent-1",
            deadline_at=participant_view.deadline_at,
            idempotency_key=(
                f"{participant_view.game_id}:{participant_view.round_id}:"
                "game-agent-1:decide"
            ),
            input_hash=sha256_identifier(participant_view),
            input=participant_view,
        )
        config = {"provider": "fake", "model": "fake-structured-v1"}

        with pytest.raises(ArenaIngressSecurityError) as captured:
            await repository.create_task(
                task=task,
                config_snapshot=config,
                config_hash=sha256_identifier(config),
                created_at=NOW,
            )

        assert raw_secret not in repr(captured.value)
        assert connection.calls == []

    asyncio.run(scenario())


def test_submit_result_hashes_runtime_id_and_uses_database_time():
    async def scenario():
        task, _ = _task()
        raw_runtime_result_id = "runtime-result-opaque-1"
        result = AgentTaskResultV1(
            result_id=raw_runtime_result_id,
            task_id=task.task_id,
            schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
            status="succeeded",
            action=BuyAction(action="buy", good="ruby"),
        )
        digest = sha256_text_identifier(raw_runtime_result_id)
        internal_id = f"runtime:{digest.removeprefix('sha256:')}"

        def handler(method, sql, args):
            assert method == "fetchrow"
            assert "submit_agent_task_result" in sql
            return {
                "disposition": "accepted",
                "authoritative_result_id": internal_id,
                "terminal_task_status": "completed",
                "result_received_at": DB_TIME,
            }

        connection = ScriptedConnection(handler)
        repository = PostgresArenaCoreRepository(
            "postgresql://unused",
            pool=FakePool(connection),
        )
        receipt = await repository.submit_result(
            result=result,
            server_clock=_must_not_run,
            message_replaced=False,
            public_output_policy_version="arena.public-output.v1",
        )

        assert receipt.disposition == SubmissionDisposition.ACCEPTED
        assert receipt.task_status == TaskStatus.COMPLETED
        assert receipt.result_received_at == DB_TIME
        _, _, parameters = connection.calls[0]
        assert parameters[1] == digest
        assert parameters[2] == sha256_identifier(result)
        assert raw_runtime_result_id not in repr(parameters)
        assert parameters[5] == {"action": "buy", "good": "ruby"}

    asyncio.run(scenario())


def test_submit_result_rejects_secret_result_id_before_sql():
    async def scenario():
        connection = ScriptedConnection(
            lambda method, sql, args: (_ for _ in ()).throw(
                AssertionError("SQL must not run")
            )
        )
        repository = PostgresArenaCoreRepository(
            "postgresql://unused",
            pool=FakePool(connection),
        )
        task, _ = _task()
        raw_secret = "sk-abcdefghijklmnopqrstuvwxyz"
        result = AgentTaskResultV1(
            result_id=raw_secret,
            task_id=task.task_id,
            schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
            status="succeeded",
            action=BuyAction(action="buy", good="ruby"),
        )

        with pytest.raises(ArenaIngressSecurityError) as captured:
            await repository.submit_result(
                result=result,
                server_clock=_must_not_run,
                message_replaced=False,
                public_output_policy_version=None,
            )

        assert raw_secret not in repr(captured.value)
        assert connection.calls == []

    asyncio.run(scenario())


def test_get_task_and_result_round_trip_json_into_strict_contracts():
    async def scenario():
        task, config = _task(negotiate=True)
        result = AgentTaskResultV1(
            result_id="runtime-result-2",
            task_id=task.task_id,
            schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
            status="succeeded",
            action=ProposeAction(
                action="propose",
                price="12.500000",
                message="Public offer.",
            ),
        )
        task_row = _task_row(
            task,
            config,
            input_snapshot=json.dumps(
                task.input.model_dump(
                    mode="json", by_alias=True, exclude_none=False
                )
            ),
            runtime_config_snapshot=json.dumps(config),
        )
        internal_id = (
            "runtime:"
            + sha256_text_identifier(result.result_id).removeprefix("sha256:")
        )
        result_row = _result_row(
            result,
            result_id=internal_id,
            candidate_action=json.dumps(
                result.action.model_dump(mode="json", by_alias=True)
            ),
        )

        def handler(method, sql, args):
            if "FROM arena_agent_tasks" in sql:
                return task_row
            if "FROM arena_agent_task_results" in sql:
                return result_row
            raise AssertionError((method, sql, args))

        repository = PostgresArenaCoreRepository(
            "postgresql://unused",
            pool=FakePool(ScriptedConnection(handler)),
        )
        stored_task = await repository.get_task(task.task_id)
        stored_result = await repository.get_result_for_task(task.task_id)

        assert stored_task is not None
        assert stored_task.task == task
        assert stored_task.config_snapshot == config
        assert stored_result is not None
        assert stored_result.result.result_id == internal_id
        assert stored_result.result.action.price == result.action.price
        assert stored_result.result.action.message == "Public offer."

    asyncio.run(scenario())


def test_finalize_and_apply_ignore_server_clock_and_map_database_rows():
    async def scenario():
        task, _ = _task()
        default_result = PostgresArenaCoreRepository._default_result(task.task_id)
        result_row = _result_row(
            default_result,
            result_hash=sha256_identifier(default_result),
            error_class="deadline_exceeded",
        )
        applied_row = {
            "task_id": task.task_id,
            "result_id": default_result.result_id,
            "task_kind": task.kind,
            "application_outcome": "default_pass",
            "applied_action": json.dumps({"action": "pass"}),
            "authoritative_entered_at": DB_TIME + timedelta(seconds=1),
            "applied_at": DB_TIME + timedelta(seconds=1),
        }

        def handler(method, sql, args):
            if method == "fetch" and "FOR UPDATE SKIP LOCKED" in sql:
                return [{"task_id": task.task_id}]
            if method == "fetchval" and "finalize_expired_agent_task" in sql:
                return True
            if method == "fetchrow" and "arena_agent_task_results" in sql:
                return result_row
            if method == "fetchval" and "apply_arena_agent_task_result" in sql:
                return True
            if method == "fetchrow" and "arena_applied_agent_actions" in sql:
                return applied_row
            raise AssertionError((method, sql, args))

        repository = PostgresArenaCoreRepository(
            "postgresql://unused",
            pool=FakePool(ScriptedConnection(handler)),
        )
        finalized = await repository.finalize_expired(
            server_clock=_must_not_run,
            limit=10,
        )
        applied = await repository.apply_result(
            result_id=default_result.result_id,
            server_clock=_must_not_run,
        )

        assert len(finalized) == 1
        assert finalized[0].result_received_at == DB_TIME
        assert finalized[0].safe_error_class == "deadline_exceeded"
        assert applied is not None
        assert applied.outcome == "default_pass"
        assert applied.action == {"action": "pass"}
        assert applied.applied_at == DB_TIME + timedelta(seconds=1)

    asyncio.run(scenario())


def test_pending_events_and_applied_actions_map_jsonb_rows():
    async def scenario():
        task, _ = _task()
        result = AgentTaskResultV1(
            result_id="runtime-result-3",
            task_id=task.task_id,
            schema_version=AGENT_TASK_RESULT_SCHEMA_VERSION_V1,
            status="succeeded",
            action=BuyAction(action="buy", good="ruby"),
        )
        internal_id = (
            "runtime:"
            + sha256_text_identifier(result.result_id).removeprefix("sha256:")
        )
        result_row = _result_row(result, result_id=internal_id)
        applied_row = {
            "task_id": task.task_id,
            "result_id": internal_id,
            "task_kind": task.kind,
            "application_outcome": "candidate",
            "applied_action": json.dumps({"action": "buy", "good": "ruby"}),
            "authoritative_entered_at": DB_TIME,
            "applied_at": DB_TIME + timedelta(seconds=1),
        }
        event_row = {
            "event_id": "event-1",
            "task_id": task.task_id,
            "event_type": "result_submitted",
            "created_at": DB_TIME,
            "safe_metadata": json.dumps(
                {"result_hash": result_row["result_hash"]}
            ),
        }

        def handler(method, sql, args):
            if "FROM arena_agent_task_results" in sql:
                return [result_row]
            if "FROM arena_agent_task_events" in sql:
                return [event_row]
            if "FROM arena_applied_agent_actions" in sql:
                return [applied_row]
            raise AssertionError((method, sql, args))

        repository = PostgresArenaCoreRepository(
            "postgresql://unused",
            pool=FakePool(ScriptedConnection(handler)),
        )
        pending = await repository.pending_results(limit=10)
        events = await repository.list_events(task.task_id)
        actions = await repository.list_applied_actions()

        assert pending[0].result.result_id == internal_id
        assert events[0].data == {"result_hash": result_row["result_hash"]}
        assert actions[0].action == {"action": "buy", "good": "ruby"}
        assert actions[0].entered_at == DB_TIME

    asyncio.run(scenario())


def test_connection_initialization_sets_role_search_path_and_json_codecs():
    class Connection:
        def __init__(self):
            self.executed = []
            self.codecs = []

        async def execute(self, sql):
            self.executed.append(sql)

        async def set_type_codec(self, name, **options):
            self.codecs.append((name, options))

    async def scenario():
        connection = Connection()

        await PostgresArenaCoreRepository._initialize_connection(connection)
        await PostgresArenaCoreRepository._setup_connection(connection)

        assert connection.executed == [
            "SET ROLE adx_arena_core",
            "SET search_path TO pg_catalog, public",
        ]
        assert [item[0] for item in connection.codecs] == ["json", "jsonb"]
        assert all(
            item[1]["schema"] == "pg_catalog" for item in connection.codecs
        )

    asyncio.run(scenario())
