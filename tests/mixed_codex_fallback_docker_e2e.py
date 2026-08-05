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
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import uuid

import asyncpg

from real_runtimes_docker_e2e import (
    ADMIN_URL,
    API_BASE,
    RealConnector,
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
}
ACTION_TIMEOUT_MS = int(
    os.getenv("ADX_MIXED_FALLBACK_E2E_ACTION_TIMEOUT_MS", "300000")
)


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
                    ) AS applied_rows
                FROM public.arena_agent_tasks AS task
                LEFT JOIN public.arena_agent_task_results AS result
                  ON result.task_id = task.task_id
                LEFT JOIN public.arena_applied_agent_actions AS applied
                  ON applied.task_id = task.task_id
                WHERE task.task_id = $1
                """,
                fault_task_id,
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
        1 if fault_mode == "disconnect_until_deadline" else 2
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
        connector = RealConnector(
            kind="codex",
            label="accepting-seller",
            user=users[2],
            credential=credential,
            connector_executable=connector_executable,
            temp_root=temp_root,
            codex_shim_root=codex_shim_root,
            run_id=run_id,
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
            await asyncio.gather(*(user.client.aclose() for user in users))


if __name__ == "__main__":
    asyncio.run(main())
