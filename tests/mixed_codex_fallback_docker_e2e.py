"""Hosted + real Codex sequential fallback through the isolated Docker stack.

This payment-disabled acceptance harness uses a scripted Hosted buyer and
rejecting seller only to make the first attempt deterministic. The remaining
seller is a real local Codex Connector Agent, so its Intent, selection, and
negotiation actions must traverse WSS, stateless MCP, AgentTaskResult, and the
Arena Result Sink. No Claude Code Runtime is started.

Set ``ADX_MIXED_FALLBACK_E2E_FAULT_MODE=restart_during_codex_select`` to
terminate the Connector while the real Codex seller is evaluating the fallback
RFQ, restart it from the same durable state, and require the same AgentTask to
be reclaimed and applied exactly once.

Set the mode to ``disconnect_until_deadline`` to leave the Connector offline
and require the Deadline Finalizer to apply ``market_timeout`` exactly once.

The ``lease_expiry_takeover`` and ``replay_terminal_outbox`` modes respectively
inject an orphan lease before MCP claim and reject one terminal Result submit
after it has been persisted in the Connector outbox.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any
import uuid

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
import asyncpg

from real_runtimes_docker_e2e import (
    ADMIN_URL,
    API_BASE,
    RealConnector,
    WS_URL,
    _build_connector,
    _create_codex_shim,
    _create_device_credential,
    _create_user,
    _require_ok,
    _wait_for_completion,
)

FAULT_MODE = os.getenv(
    "ADX_MIXED_FALLBACK_E2E_FAULT_MODE",
    "none",
).strip().lower()
VALID_FAULT_MODES = {
    "none",
    "restart_during_codex_select",
    "disconnect_until_deadline",
    "lease_expiry_takeover",
    "replay_terminal_outbox",
}
ACTION_TIMEOUT_MS = int(
    os.getenv("ADX_MIXED_FALLBACK_E2E_ACTION_TIMEOUT_MS", "300000")
)
ORPHAN_LEASE_SECONDS = int(
    os.getenv("ADX_MIXED_FALLBACK_E2E_ORPHAN_LEASE_SECONDS", "5")
)
ORPHAN_WORKER_ID = "fault-orphan-worker"


class MixedRuntimeFaultProxy:
    def __init__(
        self,
        *,
        upstream_origin: str,
        orphan_claim_injector: (
            Callable[[str], Awaitable[bool]] | None
        ) = None,
    ) -> None:
        self.upstream_origin = upstream_origin.rstrip("/")
        self._orphan_claim_injector = orphan_claim_injector
        self.origin = ""
        self._runner: web.AppRunner | None = None
        self._session: ClientSession | None = None
        self._terminal_result_task_id: str | None = None
        self._terminal_result_failed = asyncio.Event()
        self._orphan_lease_task_id: str | None = None
        self._orphan_lease_injected = asyncio.Event()

    async def start(self) -> None:
        self._session = ClientSession(timeout=ClientTimeout(total=60))
        application = web.Application(client_max_size=2 * 1024 * 1024)
        application.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(application)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        self.origin = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def fail_next_terminal_result(self, task_id: str) -> None:
        self._terminal_result_task_id = task_id
        self._terminal_result_failed.clear()

    async def wait_for_terminal_result_failure(
        self,
        *,
        timeout_seconds: float,
    ) -> None:
        await asyncio.wait_for(
            self._terminal_result_failed.wait(),
            timeout=timeout_seconds,
        )

    async def wait_for_orphan_lease_injection(
        self,
        *,
        timeout_seconds: float,
    ) -> str:
        await asyncio.wait_for(
            self._orphan_lease_injected.wait(),
            timeout=timeout_seconds,
        )
        assert self._orphan_lease_task_id is not None
        return self._orphan_lease_task_id

    async def _handle(self, request: web.Request) -> web.Response:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._bridge_websocket(request)
        body = await request.read()
        claim_task_id = self._claim_task_id(request, body)
        if (
            claim_task_id is not None
            and self._orphan_claim_injector is not None
            and await self._orphan_claim_injector(claim_task_id)
        ):
            self._orphan_lease_task_id = claim_task_id
            self._orphan_lease_injected.set()
        if self._should_fail_terminal_result(request, body):
            self._terminal_result_task_id = None
            self._terminal_result_failed.set()
            return web.Response(
                status=503,
                text="fault-injected terminal Result submission failure",
            )
        assert self._session is not None
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower()
            not in {
                "connection",
                "content-length",
                "host",
                "transfer-encoding",
            }
        }
        upstream = await self._session.request(
            request.method,
            f"{self.upstream_origin}{request.rel_url}",
            headers=headers,
            data=body,
        )
        response_body = await upstream.read()
        response_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower()
            not in {
                "connection",
                "content-encoding",
                "content-length",
                "transfer-encoding",
            }
        }
        return web.Response(
            status=upstream.status,
            headers=response_headers,
            body=response_body,
        )

    async def _bridge_websocket(
        self,
        request: web.Request,
    ) -> web.WebSocketResponse:
        assert self._session is not None
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower()
            not in {
                "connection",
                "host",
                "sec-websocket-extensions",
                "sec-websocket-key",
                "sec-websocket-version",
                "upgrade",
            }
        }
        downstream = web.WebSocketResponse()
        await downstream.prepare(request)
        upstream = await self._session.ws_connect(
            f"{self.upstream_origin}{request.rel_url}",
            headers=headers,
        )

        async def client_to_upstream() -> None:
            async for message in downstream:
                if message.type == WSMsgType.TEXT:
                    await upstream.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await upstream.send_bytes(message.data)

        async def upstream_to_client() -> None:
            async for message in upstream:
                if message.type == WSMsgType.TEXT:
                    await downstream.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await downstream.send_bytes(message.data)

        pumps = {
            asyncio.create_task(client_to_upstream()),
            asyncio.create_task(upstream_to_client()),
        }
        done, pending = await asyncio.wait(
            pumps,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
        await upstream.close()
        await downstream.close()
        return downstream

    def _should_fail_terminal_result(
        self,
        request: web.Request,
        body: bytes,
    ) -> bool:
        if (
            request.path != "/mcp"
            or self._terminal_result_task_id is None
        ):
            return False
        try:
            payload = json.loads(body)
            params = payload["params"]
            result = params["arguments"]["result"]
        except (KeyError, TypeError, json.JSONDecodeError):
            return False
        return (
            params.get("name") == "arena_submit_agent_task_result"
            and result.get("taskId") == self._terminal_result_task_id
        )

    @staticmethod
    def _claim_task_id(
        request: web.Request,
        body: bytes,
    ) -> str | None:
        if request.path != "/mcp":
            return None
        try:
            payload = json.loads(body)
            params = payload["params"]
            task_id = params["arguments"]["taskId"]
        except (KeyError, TypeError, json.JSONDecodeError):
            return None
        if (
            params.get("name") != "arena_claim_agent_task"
            or not isinstance(task_id, str)
        ):
            return None
        return task_id


async def _create_hosted_agent(
    user,
    *,
    display_name: str,
    model_id: str,
    run_id: str,
) -> str:
    credential = _require_ok(
        await user.client.post(
            "/api/model-credentials",
            headers={
                **user.mutation_headers,
                "Idempotency-Key": (
                    f"mixed-fallback-credential-{model_id}-{run_id}"
                ),
            },
            json={
                "providerId": "arena-scripted",
                "apiKey": f"development-placeholder-{run_id}-{display_name}",
            },
        )
    )
    agent = _require_ok(
        await user.client.post(
            "/api/hosted-agents",
            headers={
                **user.mutation_headers,
                "Idempotency-Key": (
                    f"mixed-fallback-agent-{model_id}-{run_id}"
                ),
            },
            json={
                "displayName": display_name,
                "credentialId": credential["credentialId"],
                "providerId": "arena-scripted",
                "modelId": model_id,
                "thinkingEnabled": False,
                "strategyInstructions": (
                    "Exercise the deterministic mixed Runtime fallback "
                    "acceptance scenario."
                ),
            },
        )
    )
    agent_id = str(agent["agentId"])
    for _ in range(120):
        current = _require_ok(
            await user.client.get(f"/api/hosted-agents/{agent_id}")
        )
        if current.get("routeStatus") == "ready":
            return agent_id
        if current.get("provisioningStatus") == "failed":
            raise RuntimeError(
                f"Hosted Agent provisioning failed: {current!r}"
            )
        await asyncio.sleep(0.25)
    raise TimeoutError(f"Hosted Agent did not become ready: {agent_id}")


async def _wait_for_leased_connector_task(
    game_id: str,
    *,
    task_kind: str,
    timeout_seconds: float = 360,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    connection = await asyncpg.connect(ADMIN_URL)
    try:
        while asyncio.get_running_loop().time() < deadline:
            row = await connection.fetchrow(
                """
                SELECT
                    task.task_id,
                    task.leased_by,
                    task.lease_expires_at,
                    task.deadline_at
                FROM public.arena_agent_tasks AS task
                JOIN public.arena_runtime_bindings AS binding
                  ON binding.runtime_binding_id = task.runtime_binding_id
                WHERE task.game_id = $1
                  AND task.task_kind = $2
                  AND binding.runtime_kind = 'connector'
                  AND task.status = 'leased'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.arena_agent_task_results AS result
                      WHERE result.task_id = task.task_id
                  )
                ORDER BY task.created_at, task.task_id
                LIMIT 1
                """,
                game_id,
                task_kind,
            )
            if row is not None:
                return dict(row)
            await asyncio.sleep(0.05)
    finally:
        await connection.close()
    raise TimeoutError(
        f"real Codex task {task_kind!r} was not observed in-flight"
    )


async def _inject_orphan_select_lease(task_id: str) -> bool:
    connection = await asyncpg.connect(ADMIN_URL)
    try:
        async with connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE public.arena_agent_tasks
                SET status = 'leased',
                    leased_by = $2,
                    lease_expires_at =
                        clock_timestamp() + $3 * interval '1 second'
                WHERE task_id = $1
                  AND task_kind = 'arena.market.select'
                  AND status = 'queued'
                  AND deadline_at >
                      clock_timestamp() + $3 * interval '1 second'
                RETURNING lease_expires_at
                """,
                task_id,
                ORPHAN_WORKER_ID,
                ORPHAN_LEASE_SECONDS,
            )
            if row is None:
                return False
            await connection.execute(
                """
                INSERT INTO public.arena_agent_task_events (
                    event_id,
                    task_id,
                    event_type,
                    created_at,
                    safe_metadata
                )
                VALUES (
                    $1,
                    $2,
                    'leased',
                    clock_timestamp(),
                    $3::jsonb
                )
                """,
                f"{task_id}:event:fault-orphan:{uuid.uuid4().hex[:12]}",
                task_id,
                json.dumps(
                    {
                        "transport": "fault-injection",
                        "worker_id": ORPHAN_WORKER_ID,
                        "lease_expires_at": row[
                            "lease_expires_at"
                        ].isoformat(),
                    },
                    separators=(",", ":"),
                ),
            )
        return True
    finally:
        await connection.close()


async def _task_lease_snapshot(task_id: str) -> dict[str, Any]:
    connection = await asyncpg.connect(ADMIN_URL)
    try:
        row = await connection.fetchrow(
            """
            SELECT
                task_id,
                leased_by,
                lease_expires_at,
                deadline_at
            FROM public.arena_agent_tasks
            WHERE task_id = $1
            """,
            task_id,
        )
    finally:
        await connection.close()
    if row is None:
        raise RuntimeError(f"fault Task disappeared: {task_id}")
    return dict(row)


def _local_outbox_result(
    connector: RealConnector,
    task_id: str,
) -> dict[str, Any] | None:
    try:
        state = json.loads(connector.state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    results = state.get("agent_task_results", {})
    if not isinstance(results, dict):
        raise RuntimeError("Connector durable Result outbox is invalid")
    value = results.get(task_id)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeError("Connector durable Result entry is invalid")
    return value


async def _wait_for_outbox_replay(
    connector: RealConnector,
    task_id: str,
    *,
    timeout_seconds: float = 60,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    connection = await asyncpg.connect(ADMIN_URL)
    try:
        while asyncio.get_running_loop().time() < deadline:
            server_results = int(
                await connection.fetchval(
                    """
                    SELECT count(*)
                    FROM public.arena_agent_task_results
                    WHERE task_id = $1
                    """,
                    task_id,
                )
            )
            if (
                server_results == 1
                and _local_outbox_result(connector, task_id) is None
            ):
                return
            await asyncio.sleep(0.05)
    finally:
        await connection.close()
    raise TimeoutError(
        f"terminal Result outbox was not replayed for {task_id}"
    )


async def _restart_during_codex_select(
    connector: RealConnector,
    game_id: str,
) -> dict[str, Any]:
    leased = await _wait_for_leased_connector_task(
        game_id,
        task_kind="arena.market.select",
    )
    await connector.stop()
    await asyncio.sleep(0.25)
    connector.start()
    runtime = await connector.wait_online()
    return {
        "faultTaskId": leased["task_id"],
        "leasedBy": leased["leased_by"],
        "leaseExpiresAt": leased["lease_expires_at"],
        "deadlineAt": leased["deadline_at"],
        "reconnectedRuntimeId": runtime["runtime_id"],
    }


async def _disconnect_until_deadline(
    connector: RealConnector,
    game_id: str,
) -> dict[str, Any]:
    leased = await _wait_for_leased_connector_task(
        game_id,
        task_kind="arena.market.select",
    )
    await connector.stop()
    return {
        "faultTaskId": leased["task_id"],
        "leasedBy": leased["leased_by"],
        "leaseExpiresAt": leased["lease_expires_at"],
        "deadlineAt": leased["deadline_at"],
        "reconnectedRuntimeId": None,
    }


async def _replay_terminal_outbox(
    proxy: MixedRuntimeFaultProxy,
    connector: RealConnector,
    game_id: str,
) -> dict[str, Any]:
    leased = await _wait_for_leased_connector_task(
        game_id,
        task_kind="arena.market.select",
    )
    task_id = str(leased["task_id"])
    proxy.fail_next_terminal_result(task_id)
    await proxy.wait_for_terminal_result_failure(timeout_seconds=360)
    await connector.stop()
    durable = _local_outbox_result(connector, task_id)
    if durable is None:
        raise RuntimeError(
            "faulted terminal Result was not durable before restart"
        )
    connection = await asyncpg.connect(ADMIN_URL)
    try:
        server_results_before_restart = int(
            await connection.fetchval(
                """
                SELECT count(*)
                FROM public.arena_agent_task_results
                WHERE task_id = $1
                """,
                task_id,
            )
        )
    finally:
        await connection.close()
    if server_results_before_restart != 0:
        raise RuntimeError(
            "faulted terminal Result reached Arena before Connector restart"
        )
    result = durable.get("result", {})
    if not isinstance(result, dict) or not result.get("resultId"):
        raise RuntimeError("durable terminal Result identity is missing")
    connector.start()
    runtime = await connector.wait_online()
    await _wait_for_outbox_replay(connector, task_id)
    return {
        "faultTaskId": task_id,
        "leasedBy": leased["leased_by"],
        "leaseExpiresAt": leased["lease_expires_at"],
        "deadlineAt": leased["deadline_at"],
        "durableResultId": result["resultId"],
        "serverResultsBeforeRestart": server_results_before_restart,
        "localOutboxBeforeRestart": 1,
        "localOutboxAfterReplay": 0,
        "reconnectedRuntimeId": runtime["runtime_id"],
    }


async def _evidence(
    game_id: str,
    *,
    fault_task_id: str | None = None,
    fault_mode: str = "none",
) -> dict[str, Any]:
    connection = await asyncpg.connect(ADMIN_URL)
    try:
        game = await connection.fetchrow(
            """
            SELECT phase, current_round, round_count, market_protocol
            FROM arena402.games
            WHERE game_id = $1
            """,
            game_id,
        )
        participants = await connection.fetch(
            """
            SELECT
                participant.game_participant_id,
                agent.name,
                binding.runtime_kind
            FROM arena402.game_participants AS participant
            JOIN public.arena_agents AS agent
              ON agent.agent_id = participant.agent_id
            JOIN public.arena_runtime_bindings AS binding
              ON binding.runtime_binding_id =
                 participant.runtime_binding_id
            WHERE participant.game_id = $1
            ORDER BY participant.joined_at, participant.game_participant_id
            """,
            game_id,
        )
        tasks = await connection.fetch(
            """
            SELECT
                task.task_id,
                task.task_kind,
                task.game_agent_id,
                task.status AS task_status,
                result.runtime_status,
                result.apply_status,
                applied.application_outcome
            FROM public.arena_agent_tasks AS task
            LEFT JOIN public.arena_agent_task_results AS result
              ON result.task_id = task.task_id
            LEFT JOIN public.arena_applied_agent_actions AS applied
              ON applied.task_id = task.task_id
            WHERE task.game_id = $1
            ORDER BY task.created_at, task.task_id
            """,
            game_id,
        )
        session = await connection.fetchrow(
            """
            SELECT status, attempt_count, max_attempts
            FROM arena402.market_rfq_sessions
            WHERE game_id = $1
            """,
            game_id,
        )
        requests = await connection.fetch(
            """
            SELECT
                request.attempt_sequence,
                request.status,
                seller.name AS seller_name
            FROM arena402.market_negotiation_requests AS request
            JOIN arena402.game_participants AS participant
              ON participant.game_participant_id =
                 request.seller_participant_id
            JOIN public.arena_agents AS seller
              ON seller.agent_id = participant.agent_id
            WHERE request.game_id = $1
            ORDER BY request.attempt_sequence
            """,
            game_id,
        )
        engagements = await connection.fetch(
            """
            SELECT status, selection_result_id
            FROM arena402.market_engagements
            WHERE game_id = $1
            ORDER BY created_at, engagement_id
            """,
            game_id,
        )
        counts = await connection.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM arena402.market_deals
                 WHERE game_id = $1) AS deals,
                (SELECT count(*) FROM arena402.pool_entries
                 WHERE game_id = $1
                   AND market_engagement_id IS NOT NULL) AS a2a_pool_entries,
                (SELECT count(*) FROM arena402.settlement_intents
                 WHERE game_id = $1) AS settlement_intents,
                (
                    SELECT count(*)
                    FROM arena402.balances AS balance
                    JOIN arena402.game_participants AS participant
                      ON participant.game_participant_id =
                         balance.game_participant_id
                    WHERE participant.game_id = $1
                      AND balance.cash_atomic <>
                          balance.initial_cash_atomic
                ) AS cash_mutations,
                (
                    SELECT count(*)
                    FROM arena402.holdings
                    WHERE game_id = $1
                      AND quantity <> initial_quantity
                ) AS holding_mutations
            """,
            game_id,
        )
        fault_task = (
            await connection.fetchrow(
                """
                SELECT
                    task.task_id,
                    task.status,
                    task.leased_by,
                    result.result_id,
                    result.runtime_status,
                    result.apply_status,
                    applied.application_outcome,
                    (
                        SELECT count(*)
                        FROM public.arena_agent_task_events AS event
                        WHERE event.task_id = task.task_id
                          AND event.event_type = 'leased'
                    ) AS lease_events,
                    (
                        SELECT count(*)
                        FROM public.arena_agent_task_results AS result
                        WHERE result.task_id = task.task_id
                    ) AS result_rows,
                    (
                        SELECT count(*)
                        FROM public.arena_applied_agent_actions AS applied
                        WHERE applied.task_id = task.task_id
                    ) AS applied_rows,
                    ARRAY(
                        SELECT DISTINCT
                            event.safe_metadata ->> 'worker_id'
                        FROM public.arena_agent_task_events AS event
                        WHERE event.task_id = task.task_id
                          AND event.event_type = 'leased'
                          AND event.safe_metadata ? 'worker_id'
                        ORDER BY
                            event.safe_metadata ->> 'worker_id'
                    ) AS lease_workers,
                    (
                        SELECT min(event.created_at)
                        FROM public.arena_agent_task_events AS event
                        WHERE event.task_id = task.task_id
                          AND event.event_type = 'leased'
                          AND event.safe_metadata ->> 'worker_id'
                              LIKE 'mcp-%'
                    ) AS mcp_takeover_at,
                    (
                        SELECT max(
                            (
                                event.safe_metadata
                                ->> 'lease_expires_at'
                            )::timestamptz
                        )
                        FROM public.arena_agent_task_events AS event
                        WHERE event.task_id = task.task_id
                          AND event.event_type = 'leased'
                          AND event.safe_metadata ->> 'worker_id' = $2
                    ) AS orphan_lease_expires_at
                FROM public.arena_agent_tasks AS task
                LEFT JOIN public.arena_agent_task_results AS result
                  ON result.task_id = task.task_id
                LEFT JOIN public.arena_applied_agent_actions AS applied
                  ON applied.task_id = task.task_id
                WHERE task.task_id = $1
                """,
                fault_task_id,
                ORPHAN_WORKER_ID,
            )
            if fault_task_id is not None
            else None
        )
    finally:
        await connection.close()

    if game is None or game["phase"] != "completed":
        raise RuntimeError(f"mixed fallback game is not completed: {game!r}")
    participant_values = [dict(row) for row in participants]
    if sorted(row["runtime_kind"] for row in participant_values) != [
        "connector",
        "hosted",
        "hosted",
    ]:
        raise RuntimeError(
            f"unexpected Runtime composition: {participant_values!r}"
        )
    if session is None or (
        session["status"],
        int(session["attempt_count"]),
        int(session["max_attempts"]),
    ) != ("completed", 2, 3):
        raise RuntimeError(f"unexpected RFQ session: {dict(session or {})!r}")
    request_values = [dict(row) for row in requests]
    if len(request_values) != 2:
        raise RuntimeError(f"expected two RFQs: {request_values!r}")
    if "Rejecting" not in request_values[0]["seller_name"]:
        raise RuntimeError(
            f"first RFQ did not target rejecting seller: {request_values!r}"
        )
    if "Codex" not in request_values[1]["seller_name"]:
        raise RuntimeError(
            f"fallback RFQ did not target Codex seller: {request_values!r}"
        )
    expected_request_statuses = (
        ["rejected", "expired"]
        if fault_mode == "disconnect_until_deadline"
        else ["rejected", "engaged"]
    )
    if [row["status"] for row in request_values] != expected_request_statuses:
        raise RuntimeError(
            f"unexpected fallback RFQ outcomes: {request_values!r}"
        )
    engagement_values = [dict(row) for row in engagements]
    expected_engagement_statuses = (
        ["rejected"]
        if fault_mode == "disconnect_until_deadline"
        else ["rejected", "settlement_failed"]
    )
    if [row["status"] for row in engagement_values] != expected_engagement_statuses:
        raise RuntimeError(
            f"unexpected Engagement outcomes: {engagement_values!r}"
        )
    count_values = dict(counts)
    expected_counts = {
        "deals": 0 if fault_mode == "disconnect_until_deadline" else 1,
        "a2a_pool_entries": 2 if fault_mode == "disconnect_until_deadline" else 4,
        "settlement_intents": 0,
        "cash_mutations": 0,
        "holding_mutations": 0,
    }
    if count_values != expected_counts:
        raise RuntimeError(
            f"mixed fallback violated payment boundary: {count_values!r}"
        )
    task_values = [dict(row) for row in tasks]
    invalid_tasks = [
        row
        for row in task_values
        if not (
            row["apply_status"] == "applied"
            and (
                (
                    fault_mode == "disconnect_until_deadline"
                    and row["task_id"] == fault_task_id
                    and row["task_status"] == "defaulted"
                    and row["runtime_status"] == "timed_out"
                    and row["application_outcome"] == "market_timeout"
                )
                or (
                    row["task_id"] != fault_task_id
                    and row["task_status"] == "completed"
                    and row["runtime_status"] == "succeeded"
                    and row["application_outcome"] == "candidate"
                )
                or (
                    fault_mode != "disconnect_until_deadline"
                    and row["task_id"] == fault_task_id
                    and row["task_status"] == "completed"
                    and row["runtime_status"] == "succeeded"
                    and row["application_outcome"] == "candidate"
                )
            )
        )
    ]
    if invalid_tasks:
        raise RuntimeError(
            f"task lacks expected applied result: {invalid_tasks!r}"
        )
    fault_task_value = dict(fault_task) if fault_task is not None else None
    expected_fault_status = (
        "defaulted"
        if fault_mode == "disconnect_until_deadline"
        else "completed"
    )
    minimum_lease_events = (
        2
        if fault_mode
        in {"restart_during_codex_select", "lease_expiry_takeover"}
        else 1
    )
    if fault_task_id is not None and (
        fault_task_value is None
        or fault_task_value["status"] != expected_fault_status
        or int(fault_task_value["lease_events"]) < minimum_lease_events
        or int(fault_task_value["result_rows"]) != 1
        or int(fault_task_value["applied_rows"]) != 1
    ):
        raise RuntimeError(
            "fault-injected Codex task did not reach its exact-once outcome: "
            f"{fault_task_value!r}"
        )
    if fault_mode == "lease_expiry_takeover":
        workers = set(fault_task_value["lease_workers"])
        if (
            ORPHAN_WORKER_ID not in workers
            or not any(worker.startswith("mcp-") for worker in workers)
            or fault_task_value["orphan_lease_expires_at"] is None
            or fault_task_value["mcp_takeover_at"] is None
            or fault_task_value["mcp_takeover_at"]
            < fault_task_value["orphan_lease_expires_at"]
        ):
            raise RuntimeError(
                "orphan lease was not taken over after expiry: "
                f"{fault_task_value!r}"
            )
    evidence = {
        "game": dict(game),
        "participants": participant_values,
        "rfqSession": dict(session),
        "requests": request_values,
        "engagements": engagement_values,
        "counts": count_values,
        "taskCount": len(task_values),
        "taskKinds": [row["task_kind"] for row in task_values],
    }
    if fault_task_value is not None:
        evidence["faultTask"] = fault_task_value
    return evidence


async def main() -> None:
    if FAULT_MODE not in VALID_FAULT_MODES:
        raise RuntimeError(
            "ADX_MIXED_FALLBACK_E2E_FAULT_MODE must be one of "
            f"{sorted(VALID_FAULT_MODES)!r}"
        )
    if ACTION_TIMEOUT_MS < 1_000 or ACTION_TIMEOUT_MS > 900_000:
        raise RuntimeError(
            "ADX_MIXED_FALLBACK_E2E_ACTION_TIMEOUT_MS must be between "
            "1000 and 900000"
        )
    if FAULT_MODE == "lease_expiry_takeover" and (
        ORPHAN_LEASE_SECONDS < 1
        or ORPHAN_LEASE_SECONDS > 30
        or ACTION_TIMEOUT_MS <= (ORPHAN_LEASE_SECONDS + 5) * 1_000
    ):
        raise RuntimeError(
            "orphan lease must be 1..30 seconds and leave at least five "
            "seconds before the action deadline"
        )
    invites = json.loads(os.environ["ADX_MIXED_FALLBACK_E2E_INVITES"])
    if isinstance(invites, dict):
        invites = invites.get("invites")
    if (
        not isinstance(invites, list)
        or len(invites) != 3
        or not all(isinstance(value, str) for value in invites)
    ):
        raise RuntimeError(
            "ADX_MIXED_FALLBACK_E2E_INVITES must contain three invites"
        )

    run_id = uuid.uuid4().hex[:10]
    users = tuple(
        await asyncio.gather(
            *(
                _create_user(invites[index], f"mixed_seat{index + 1}", run_id)
                for index in range(3)
            )
        )
    )
    with tempfile.TemporaryDirectory(
        prefix="arena402-mixed-fallback-"
    ) as temporary:
        temp_root = Path(temporary)
        connector_executable = temp_root / (
            "adx-connector-e2e.exe" if os.name == "nt" else "adx-connector-e2e"
        )
        _build_connector(connector_executable)
        codex_shim_root = temp_root / "codex-shim"
        _create_codex_shim(codex_shim_root)
        credential = await _create_device_credential(
            users[2],
            device_name="Real Codex mixed fallback seller",
        )
        proxy: MixedRuntimeFaultProxy | None = None
        gateway_url = WS_URL
        if FAULT_MODE in {
            "lease_expiry_takeover",
            "replay_terminal_outbox",
        }:
            proxy = MixedRuntimeFaultProxy(
                upstream_origin=API_BASE,
                orphan_claim_injector=(
                    _inject_orphan_select_lease
                    if FAULT_MODE == "lease_expiry_takeover"
                    else None
                ),
            )
            await proxy.start()
            gateway_url = (
                proxy.origin.replace("http://", "ws://", 1)
                + "/api/connectors/ws"
            )
        connector = RealConnector(
            kind="codex",
            label="accepting-seller",
            user=users[2],
            credential=credential,
            connector_executable=connector_executable,
            temp_root=temp_root,
            codex_shim_root=codex_shim_root,
            run_id=run_id,
            gateway_url=gateway_url,
        )
        game_id = f"mixed-fallback-{run_id}"
        try:
            connector.start()
            connector_binding = await connector.wait_online_and_bind()
            buyer_agent_id, rejecting_seller_agent_id = await asyncio.gather(
                _create_hosted_agent(
                    users[0],
                    display_name="Mixed Fallback Buyer",
                    model_id="arena-fallback-buyer-v1",
                    run_id=run_id,
                ),
                _create_hosted_agent(
                    users[1],
                    display_name="Mixed Rejecting Seller",
                    model_id="arena-rejecting-seller-v1",
                    run_id=run_id,
                ),
            )
            _require_ok(
                await users[0].client.post(
                    "/api/v1/pawnhouse/games",
                    headers=users[0].mutation_headers,
                    json={
                        "gameId": game_id,
                        "eventSeed": "mixed-codex-fallback-iron",
                        "actionTimeoutMs": ACTION_TIMEOUT_MS,
                        "roundCount": 1,
                        "eventMode": "seeded_shuffle",
                        "marketProtocol": "agent_a2a.v1",
                        "maxParticipants": 3,
                        "portfolioMode": "manual",
                        "settlement": {"authorizationMode": "none"},
                    },
                )
            )
            joins = await asyncio.gather(
                users[0].client.post(
                    f"/api/v1/pawnhouse/games/{game_id}/hosted-participants",
                    headers=users[0].mutation_headers,
                    json={
                        "agentId": buyer_agent_id,
                        "portfolio": {
                            "cash": "20.000000",
                            "holdings": {},
                        },
                    },
                ),
                users[1].client.post(
                    f"/api/v1/pawnhouse/games/{game_id}/hosted-participants",
                    headers=users[1].mutation_headers,
                    json={
                        "agentId": rejecting_seller_agent_id,
                        "portfolio": {
                            "cash": "15.000000",
                            "holdings": {"iron": 1},
                        },
                    },
                ),
                users[2].client.post(
                    f"/api/v1/pawnhouse/games/{game_id}/connector-participants",
                    headers=users[2].mutation_headers,
                    json={
                        "agentId": connector_binding["agent_id"],
                        "portfolio": {
                            "cash": "0.000000",
                            "holdings": {"iron": 4},
                        },
                    },
                ),
            )
            for response in joins:
                _require_ok(response)
            _require_ok(
                await users[0].client.post(
                    f"/api/v1/pawnhouse/games/{game_id}/start",
                    headers=users[0].mutation_headers,
                )
            )
            fault_evidence: dict[str, Any] | None = None
            if FAULT_MODE == "restart_during_codex_select":
                fault_evidence = await _restart_during_codex_select(
                    connector,
                    game_id,
                )
            elif FAULT_MODE == "disconnect_until_deadline":
                fault_evidence = await _disconnect_until_deadline(
                    connector,
                    game_id,
                )
            elif FAULT_MODE == "lease_expiry_takeover":
                assert proxy is not None
                task_id = await proxy.wait_for_orphan_lease_injection(
                    timeout_seconds=360,
                )
                leased = await _task_lease_snapshot(task_id)
                fault_evidence = {
                    "faultTaskId": task_id,
                    "leasedBy": leased["leased_by"],
                    "leaseExpiresAt": leased["lease_expires_at"],
                    "deadlineAt": leased["deadline_at"],
                    "orphanLeaseSeconds": ORPHAN_LEASE_SECONDS,
                    "reconnectedRuntimeId": None,
                }
            elif FAULT_MODE == "replay_terminal_outbox":
                assert proxy is not None
                fault_evidence = await _replay_terminal_outbox(
                    proxy,
                    connector,
                    game_id,
                )
            live_connectors = (
                ()
                if FAULT_MODE == "disconnect_until_deadline"
                else (connector,)
            )
            final_state = await _wait_for_completion(
                users[0],
                game_id,
                live_connectors,
            )
            evidence = await _evidence(
                game_id,
                fault_task_id=(
                    str(fault_evidence["faultTaskId"])
                    if fault_evidence is not None
                    else None
                ),
                fault_mode=FAULT_MODE,
            )
            if (
                FAULT_MODE == "replay_terminal_outbox"
                and not evidence["faultTask"]["result_id"]
            ):
                raise RuntimeError(
                    "Arena did not assign an authoritative Result identity"
                )
            if FAULT_MODE == "replay_terminal_outbox":
                fault_evidence["arenaAuthoritativeResultId"] = evidence[
                    "faultTask"
                ]["result_id"]
            print(
                json.dumps(
                    {
                        "gameId": game_id,
                        "finalPhase": final_state["phase"],
                        "marketProtocol": "agent_a2a.v1",
                        "runtimeComposition": [
                            "hosted-scripted",
                            "hosted-scripted",
                            "connector-codex",
                        ],
                        "codexVersion": connector.runtime["version"],
                        "taskTransport": "mcp",
                        "evidenceClass": "mixed_real_codex_connector",
                        "faultMode": FAULT_MODE,
                        "faultInjection": fault_evidence,
                        "evidence": evidence,
                        "chainWrites": 0,
                    },
                    separators=(",", ":"),
                    default=str,
                )
            )
        except Exception as exc:
            raise RuntimeError(
                f"{exc}\nCodex Connector logs:\n"
                f"{connector._failure_logs()}"
            ) from exc
        finally:
            await connector.stop()
            if proxy is not None:
                await proxy.stop()
            await asyncio.gather(*(user.client.aclose() for user in users))


if __name__ == "__main__":
    asyncio.run(main())
