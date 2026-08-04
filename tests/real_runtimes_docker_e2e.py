"""Real local Runtime game through Docker Arena and stateless MCP.

Prerequisites:

* an isolated Compose stack using ``docker-compose.real-runtimes-e2e.yml``;
* two one-time invites in ``ADX_REAL_RUNTIME_E2E_INVITES`` as a JSON array;
* locally authenticated CLIs selected by ``ADX_REAL_RUNTIME_E2E_RUNTIME_KINDS``;
* Go available to build the current Connector source.

Arena, Gateway, PostgreSQL, and the Arena worker run in Docker. Two independent
Connectors and their managed Runtime children run on the host so existing local
CLI authentication never enters a container. The default is one Claude Code
and one Codex Runtime; ``codex,codex`` runs a Codex-only game with separate
users, devices, bindings, sessions, and state stores. Payment support is
disabled and the game freezes ``authorizationMode=none``; an accepted
negotiation must close as ``settlement_failed`` without moving inventory or
writing to chain.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any, Literal
import uuid

import asyncpg
import httpx


ROOT = Path(__file__).resolve().parents[1]
API_BASE = os.getenv(
    "ADX_REAL_RUNTIME_E2E_API_BASE",
    "http://127.0.0.1:18001",
)
WS_URL = os.getenv(
    "ADX_REAL_RUNTIME_E2E_WS_URL",
    "ws://127.0.0.1:18001/api/connectors/ws",
)
ADMIN_URL = os.getenv(
    "ADX_REAL_RUNTIME_E2E_ADMIN_URL",
    "postgresql://arena402_admin:arena402-local-admin-password"
    "@127.0.0.1:55434/arena402",
)
GAME_TIMEOUT_SECONDS = int(
    os.getenv("ADX_REAL_RUNTIME_E2E_GAME_TIMEOUT_SECONDS", "720")
)
ACTION_TIMEOUT_MS = int(os.getenv("ADX_REAL_RUNTIME_E2E_ACTION_TIMEOUT_MS", "300000"))
ROUND_COUNT = int(os.getenv("ADX_REAL_RUNTIME_E2E_ROUND_COUNT", "1"))
EVENT_SEED = os.getenv(
    "ADX_REAL_RUNTIME_E2E_EVENT_SEED",
    "real-runtime-grain-1",
)
PUBLIC_EVIDENCE_ONLY = os.getenv(
    "ADX_REAL_RUNTIME_E2E_PUBLIC_EVIDENCE_ONLY",
    "",
).strip().lower() in {"1", "true", "yes"}
SAFE_NO_TRADE = os.getenv(
    "ADX_REAL_RUNTIME_E2E_SAFE_NO_TRADE",
    "",
).strip().lower() in {"1", "true", "yes"}
EXPECT_MATCH = os.getenv(
    "ADX_REAL_RUNTIME_E2E_EXPECT_MATCH",
    "",
).strip().lower() in {"1", "true", "yes"}
EXPECT_DEAL = os.getenv(
    "ADX_REAL_RUNTIME_E2E_EXPECT_DEAL",
    "",
).strip().lower() in {"1", "true", "yes"}
BUYER_RUNTIME = os.getenv(
    "ADX_REAL_RUNTIME_E2E_BUYER_RUNTIME",
    "claude-code",
).strip().lower()
RUNTIME_KINDS = tuple(
    value.strip().lower()
    for value in os.getenv(
        "ADX_REAL_RUNTIME_E2E_RUNTIME_KINDS",
        "claude-code,codex",
    ).split(",")
    if value.strip()
)
BUYER_SEAT = int(os.getenv("ADX_REAL_RUNTIME_E2E_BUYER_SEAT", "-1"))
MARKET_PROTOCOL = os.getenv(
    "ADX_REAL_RUNTIME_E2E_MARKET_PROTOCOL",
    "fcfs.v1",
).strip()


def _require_ok(response: httpx.Response) -> dict[str, Any]:
    if response.is_error:
        raise RuntimeError(
            f"{response.request.method} {response.request.url.path} failed: "
            f"HTTP {response.status_code} {response.text[:1000]}"
        )
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError("expected an object response")
    return value


async def _read_json_with_retry(
    client: httpx.AsyncClient,
    path: str,
    *,
    attempts: int = 10,
) -> dict[str, Any]:
    for attempt in range(1, attempts + 1):
        try:
            response = await client.get(path)
        except httpx.TransportError:
            if attempt == attempts:
                raise
        else:
            if response.status_code not in {502, 503, 504}:
                return _require_ok(response)
            if attempt == attempts:
                return _require_ok(response)
        await asyncio.sleep(min(0.25 * attempt, 1.0))
    raise AssertionError("bounded read retry exhausted without a result")


def _require_command(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise RuntimeError(f"required local command is unavailable: {name}")
    return str(Path(resolved).resolve())


def _build_connector(target: Path) -> None:
    go = _require_command("go")
    result = subprocess.run(
        [go, "build", "-o", str(target), "./cmd/adx-connector"],
        cwd=ROOT / "connector",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Connector build failed:\n{result.stdout[-4000:]}")


def _create_codex_shim(directory: Path) -> Path:
    override = os.getenv("ADX_REAL_RUNTIME_E2E_CODEX_COMMAND", "").strip()
    if override:
        original_path = Path(override).expanduser().resolve()
        if not original_path.is_file():
            raise RuntimeError(
                "ADX_REAL_RUNTIME_E2E_CODEX_COMMAND does not name a file"
            )
        original = str(original_path)
    else:
        original = _require_command("codex")
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        shim = directory / "codex.cmd"
        shim.write_text(
            "@echo off\r\n"
            f'call "{original}" -c service_tier=fast '
            "-s read-only -a never %*\r\n",
            encoding="utf-8",
        )
    else:
        shim = directory / "codex"
        shim.write_text(
            "#!/bin/sh\n"
            f'exec "{original}" -c service_tier=fast '
            '-s read-only -a never "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return shim


@dataclass(slots=True)
class UserSession:
    client: httpx.AsyncClient
    csrf_token: str

    @property
    def mutation_headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf_token}


async def _create_user(invite: str, label: str, run_id: str) -> UserSession:
    client = httpx.AsyncClient(base_url=API_BASE, timeout=30)
    auth = _require_ok(
        await client.post(
            "/api/auth/invite",
            json={
                "invite_code": invite,
                "username": f"real_runtime_{label}_{run_id}",
                "password": f"Arena402-real-runtime-{label}-{run_id}!",
            },
        )
    )
    return UserSession(client=client, csrf_token=auth["csrf_token"])


async def _create_device_credential(
    user: UserSession,
    *,
    device_name: str,
) -> dict[str, str]:
    pairing = _require_ok(
        await user.client.post(
            "/api/connectors/pairings",
            json={"device_name": device_name},
        )
    )
    _require_ok(
        await user.client.post(
            f"/api/connectors/pairings/{pairing['user_code']}/approve",
            headers=user.mutation_headers,
            json={},
        )
    )
    credential = _require_ok(
        await user.client.post(
            "/api/connectors/pairings/exchange",
            json={"device_code": pairing["device_code"]},
        )
    )
    return {
        "device_id": credential["device_id"],
        "device_token": credential["device_token"],
    }


class RealConnector:
    def __init__(
        self,
        *,
        kind: Literal["claude-code", "codex"],
        label: str,
        user: UserSession,
        credential: dict[str, str],
        connector_executable: Path,
        temp_root: Path,
        codex_shim_root: Path,
        run_id: str,
    ) -> None:
        self.kind = kind
        self.label = label
        self.user = user
        self.credential = credential
        self.connector_executable = connector_executable
        self.temp_root = temp_root
        self.codex_shim_root = codex_shim_root
        self.run_id = run_id
        self.process: subprocess.Popen[str] | None = None
        self.logs: deque[str] = deque(maxlen=400)
        self.runtime: dict[str, Any] = {}
        self.binding: dict[str, Any] = {}

    def start(self) -> None:
        environment = os.environ.copy()
        environment["ADX_CONNECTOR_DEVICE_ID"] = self.credential["device_id"]
        environment["ADX_CONNECTOR_TOKEN"] = self.credential["device_token"]
        environment["ADX_CONNECTOR_GATEWAY_URL"] = WS_URL
        if self.kind == "codex":
            environment["PATH"] = (
                f"{self.codex_shim_root}{os.pathsep}" f"{environment.get('PATH', '')}"
            )
        connector_state_root = self.temp_root / self.label
        connector_state_root.mkdir(parents=True, exist_ok=True)
        arguments = [
            str(self.connector_executable),
            "run",
            "--server",
            API_BASE,
            "--gateway",
            WS_URL,
            "--task-transport",
            "mcp",
            "--auto-pair=false",
            "--state",
            str(connector_state_root / f"state-{self.run_id}.json"),
            "--allow-root",
            str(ROOT),
            "--heartbeat",
            "1s",
            "--inventory-interval",
            "30s",
            "--discovery-timeout",
            "10s",
        ]
        if self.kind == "codex":
            arguments.append("--enable-codex-tasks")
        else:
            arguments.append("--unsafe-enable-claude-tasks")
        self.process = subprocess.Popen(
            arguments,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert self.process.stdout is not None

        def drain() -> None:
            assert self.process is not None
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.logs.append(line.rstrip())

        threading.Thread(target=drain, daemon=True).start()

    def _failure_logs(self) -> str:
        return "\n".join(list(self.logs)[-100:])

    async def wait_online_and_bind(self) -> dict[str, Any]:
        assert self.process is not None
        device_id = self.credential["device_id"]
        for _ in range(240):
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"{self.label} ({self.kind}) Connector exited before readiness:\n"
                    f"{self._failure_logs()}"
                )
            response = await self.user.client.get(
                f"/api/connectors/devices/{device_id}"
            )
            if response.status_code == 200:
                device = _require_ok(response)
                for runtime in device.get("runtimes", []):
                    capabilities = set(runtime.get("capabilities", []))
                    if (
                        runtime.get("kind") == self.kind
                        and runtime.get("available") is True
                        and runtime.get("task_enabled") is True
                        and runtime.get("authentication_status") == "configured"
                        and runtime.get("arena_compatible") is True
                        and runtime.get("local_execution_ready") is True
                        and {"session.start", "task.dispatch"} <= capabilities
                    ):
                        self.runtime = runtime
                        self.binding = _require_ok(
                            await self.user.client.post(
                                f"/api/connectors/devices/{device_id}/bindings",
                                headers=self.user.mutation_headers,
                                json={
                                    "runtime_id": runtime["runtime_id"],
                                    "display_name": (
                                        f"Real Claude Code {self.label}"
                                        if self.kind == "claude-code"
                                        else f"Real Codex {self.label}"
                                    ),
                                    "working_directory": str(ROOT),
                                },
                            )
                        )
                        registration = self.binding.get("arenaRegistration", {})
                        if registration.get("routeStatus") != "ready":
                            raise RuntimeError(
                                f"{self.label} ({self.kind}) Arena route is not ready: "
                                f"{registration!r}"
                            )
                        return self.binding
            await asyncio.sleep(0.25)
        raise RuntimeError(
            f"{self.label} ({self.kind}) Connector did not publish a locally "
            "ready Runtime:\n"
            f"{self._failure_logs()}"
        )

    async def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            await asyncio.to_thread(self.process.wait, 20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            await asyncio.to_thread(self.process.wait, 20)


async def _wait_for_completion(
    user: UserSession,
    game_id: str,
    connectors: tuple[RealConnector, ...],
) -> dict[str, Any]:
    deadline = time.monotonic() + GAME_TIMEOUT_SECONDS
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_state = await _read_json_with_retry(
            user.client,
            f"/api/v1/pawnhouse/games/{game_id}",
        )
        if last_state.get("phase") == "completed":
            return last_state
        for connector in connectors:
            assert connector.process is not None
            exit_code = connector.process.poll()
            if exit_code is not None:
                raise RuntimeError(
                    f"{connector.label} ({connector.kind}) Connector exited "
                    f"with {exit_code}:\n"
                    f"{connector._failure_logs()}"
                )
        await asyncio.sleep(0.5)
    logs = "\n\n".join(
        f"[{connector.label}:{connector.kind}]\n{connector._failure_logs()}"
        for connector in connectors
    )
    raise RuntimeError(
        f"game did not complete before timeout; last state={last_state!r}\n" f"{logs}"
    )


async def _database_evidence(game_id: str) -> dict[str, Any]:
    connection = await asyncpg.connect(ADMIN_URL)
    try:
        game = await connection.fetchrow(
            """
            SELECT
                phase, current_round, round_count, market_protocol,
                config_snapshot
            FROM arena402.games
            WHERE game_id = $1
            """,
            game_id,
        )
        participants = await connection.fetch(
            """
            SELECT
                p.game_participant_id,
                a.name,
                b.runtime_kind,
                b.connector_binding_id,
                b.connector_binding_epoch
            FROM arena402.game_participants AS p
            JOIN public.arena_agents AS a ON a.agent_id = p.agent_id
            JOIN public.arena_runtime_bindings AS b
              ON b.runtime_binding_id = p.runtime_binding_id
            WHERE p.game_id = $1
            ORDER BY p.joined_at, p.game_participant_id
            """,
            game_id,
        )
        tasks = await connection.fetch(
            """
            SELECT
                t.task_id,
                t.task_kind AS kind,
                t.game_agent_id,
                t.status AS task_status,
                t.terminal_reason,
                r.result_id,
                r.runtime_status,
                r.candidate_action,
                r.apply_status,
                a.application_outcome,
                a.applied_action
            FROM public.arena_agent_tasks AS t
            LEFT JOIN public.arena_agent_task_results AS r
              ON r.task_id = t.task_id
            LEFT JOIN public.arena_applied_agent_actions AS a
              ON a.task_id = t.task_id
            WHERE t.game_id = $1
            ORDER BY t.created_at, t.task_id
            """,
            game_id,
        )
        decisions = await connection.fetch(
            """
            SELECT public_payload
            FROM arena402.game_events
            WHERE game_id = $1
              AND event_type = 'decision.applied'
            ORDER BY created_at, event_sequence
            """,
            game_id,
        )
        negotiation_rows = await connection.fetch(
            """
            SELECT negotiation_id, status, turn_count, accepted_price_atomic
            FROM arena402.negotiations
            WHERE game_id = $1
            ORDER BY negotiation_id
            """,
            game_id,
        )
        deal_rows = await connection.fetch(
            """
            SELECT
                deal_id,
                engagement_id,
                request_id,
                round_id,
                buyer_participant_id,
                seller_participant_id,
                good_id,
                quantity,
                unit_price_atomic,
                latest_proposal_result_id,
                acceptance_result_id,
                accepted_by_participant_id,
                created_at
            FROM arena402.market_deals
            WHERE game_id = $1
            ORDER BY created_at, deal_id
            """,
            game_id,
        )
        counts = await connection.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM arena402.pairings
                 WHERE game_id = $1) AS pairings,
                (SELECT count(*) FROM arena402.negotiation_messages
                 WHERE game_id = $1) AS negotiation_messages,
                (SELECT count(*) FROM arena402.settlement_intents
                 WHERE game_id = $1) AS settlement_intents,
                (SELECT count(*) FROM arena402.market_intents
                 WHERE game_id = $1) AS market_intents,
                (SELECT count(*) FROM arena402.market_negotiation_requests
                 WHERE game_id = $1) AS market_requests,
                (SELECT count(*) FROM arena402.market_engagements
                 WHERE game_id = $1) AS market_engagements,
                (SELECT count(*) FROM arena402.market_deals
                 WHERE game_id = $1) AS market_deals,
                (
                    SELECT count(*)
                    FROM arena402.inventory_commits AS inventory_commit
                    JOIN arena402.settlement_intents AS intent
                      ON intent.settlement_intent_id =
                         inventory_commit.settlement_intent_id
                    WHERE intent.game_id = $1
                ) AS inventory_commits,
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
                    FROM arena402.holdings AS holding
                    WHERE holding.game_id = $1
                      AND holding.quantity <>
                          holding.initial_quantity
                ) AS holding_mutations,
                (SELECT count(*) FROM arena402.rankings
                 WHERE game_id = $1) AS rankings
            """,
            game_id,
        )
    finally:
        await connection.close()
    if game is None:
        raise RuntimeError("game disappeared from PostgreSQL")

    task_values = [dict(row) for row in tasks]
    participant_values = [dict(row) for row in participants]
    if game["phase"] != "completed":
        raise RuntimeError(f"database game is not completed: {dict(game)!r}")
    if len(participant_values) != 2:
        raise RuntimeError(
            f"expected two Connector participants: {participant_values!r}"
        )
    if int(counts["rankings"]) != 2:
        raise RuntimeError(f"expected two rankings: {dict(counts)!r}")
    opening_kind = (
        "arena.market.intent"
        if MARKET_PROTOCOL == "agent_a2a.v1"
        else "arena.decide"
    )
    opening_tasks = [
        row for row in task_values if row["kind"] == opening_kind
    ]
    expected_opening_tasks = 2 * ROUND_COUNT
    if len(opening_tasks) != expected_opening_tasks:
        raise RuntimeError(
            f"expected {expected_opening_tasks} real {opening_kind} tasks: "
            f"{task_values!r}"
        )
    for task in task_values:
        if (
            task["task_status"] != "completed"
            or task["runtime_status"] != "succeeded"
            or task["apply_status"] != "applied"
            or task["application_outcome"] != "candidate"
            or task["candidate_action"] is None
            or task["terminal_reason"] not in {None, "runtime_result"}
        ):
            raise RuntimeError(f"task lacks a real applied result: {task!r}")
    if int(counts["settlement_intents"]) != 0:
        raise RuntimeError(
            f"no-payment E2E created settlement intents: {dict(counts)!r}"
        )
    if (
        int(counts["inventory_commits"]) != 0
        or int(counts["cash_mutations"]) != 0
        or int(counts["holding_mutations"]) != 0
    ):
        raise RuntimeError(
            "no-payment E2E moved authoritative inventory: "
            f"{dict(counts)!r}"
        )
    if EXPECT_MATCH and (
        int(counts["pairings"]) < 1
        or int(counts["negotiation_messages"]) < 1
    ):
        raise RuntimeError(
            f"trade probe did not reach negotiation: {dict(counts)!r}"
        )
    deal_values = [dict(row) for row in deal_rows]
    for deal in deal_values:
        if (
            deal["latest_proposal_result_id"]
            == deal["acceptance_result_id"]
            or int(deal["quantity"]) != 1
            or int(deal["unit_price_atomic"]) <= 0
        ):
            raise RuntimeError(
                f"Deal lacks immutable Agent Result provenance: {deal!r}"
            )
    if EXPECT_DEAL and not deal_values:
        raise RuntimeError(
            f"real-Agent probe completed without a Deal: {dict(counts)!r}"
        )

    config = game["config_snapshot"]
    if isinstance(config, str):
        config = json.loads(config)
    return {
        "game": {
            "phase": game["phase"],
            "currentRound": int(game["current_round"]),
            "roundCount": int(game["round_count"]),
            "marketProtocol": str(game["market_protocol"]),
            "authorizationMode": dict(config)
            .get("settlement", {})
            .get("authorizationMode"),
        },
        "participants": participant_values,
        "tasks": task_values,
        "decisions": [row["public_payload"] for row in decisions],
        "negotiations": [dict(row) for row in negotiation_rows],
        "deals": deal_values,
        "counts": dict(counts),
    }


async def _public_evidence(
    user: UserSession,
    game_id: str,
) -> dict[str, Any]:
    state = await _read_json_with_retry(
        user.client,
        f"/api/v1/pawnhouse/games/{game_id}",
    )
    timeline = await _read_json_with_retry(
        user.client,
        f"/api/v1/pawnhouse/games/{game_id}/timeline",
    )
    runtime_run = await _read_json_with_retry(
        user.client,
        f"/api/v1/pawnhouse/games/{game_id}/runtime-run",
    )
    settlements = await _read_json_with_retry(
        user.client,
        f"/api/v1/pawnhouse/games/{game_id}/settlement-intents",
    )
    if state.get("phase") != "completed":
        raise RuntimeError(f"public game state is not completed: {state!r}")
    if len(state.get("participants", [])) != 2:
        raise RuntimeError(f"expected two public participants: {state!r}")
    if len(state.get("rankings", [])) != 2:
        raise RuntimeError(f"expected two public rankings: {state!r}")
    if runtime_run.get("status") != "completed":
        raise RuntimeError(
            f"public Runtime Run is not completed: {runtime_run!r}"
        )
    if SAFE_NO_TRADE and settlements.get("total") != 0:
        raise RuntimeError(
            f"safe no-trade game created a settlement intent: {settlements!r}"
        )
    if EXPECT_MATCH and not state.get("pairings"):
        raise RuntimeError(
            f"public trade probe did not create a pairing: {state!r}"
        )
    return {
        "game": state,
        "timeline": timeline,
        "runtimeRun": runtime_run,
        "settlementIntents": settlements,
    }


def _probe_mcp_data_plane() -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {
                    "name": "arena-real-runtime-e2e",
                    "version": "1.0",
                },
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    response = httpx.post(
        f"{API_BASE}/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "MCP-Method": "server/discover",
        },
        json=body,
        timeout=15,
    )
    if response.status_code != 401:
        raise RuntimeError(
            "Arena MCP data plane probe did not reach the authenticated "
            f"endpoint: HTTP {response.status_code} {response.text[:1000]}"
        )


async def main() -> None:
    invites = json.loads(os.environ["ADX_REAL_RUNTIME_E2E_INVITES"])
    if (
        not isinstance(invites, list)
        or len(invites) != 2
        or not all(isinstance(value, str) for value in invites)
    ):
        raise RuntimeError(
            "ADX_REAL_RUNTIME_E2E_INVITES must be a JSON array of two invites"
        )
    health = httpx.get(f"{API_BASE}/api/health", timeout=15)
    health.raise_for_status()
    if health.json().get("arena_mcp") is not True:
        _probe_mcp_data_plane()
    if ROUND_COUNT < 1 or ROUND_COUNT > 10:
        raise RuntimeError(
            "ADX_REAL_RUNTIME_E2E_ROUND_COUNT must be between 1 and 10"
        )
    if len(RUNTIME_KINDS) != 2 or any(
        value not in {"claude-code", "codex"} for value in RUNTIME_KINDS
    ):
        raise RuntimeError(
            "ADX_REAL_RUNTIME_E2E_RUNTIME_KINDS must contain exactly two "
            "comma-separated values chosen from claude-code and codex"
        )
    buyer_seat = BUYER_SEAT
    if buyer_seat == -1:
        matches = [
            index
            for index, runtime_kind in enumerate(RUNTIME_KINDS)
            if runtime_kind == BUYER_RUNTIME
        ]
        if len(matches) == 1:
            buyer_seat = matches[0]
        elif len(set(RUNTIME_KINDS)) == 1:
            buyer_seat = 0
        else:
            raise RuntimeError(
                "ADX_REAL_RUNTIME_E2E_BUYER_RUNTIME must identify one Runtime "
                "or ADX_REAL_RUNTIME_E2E_BUYER_SEAT must be 0 or 1"
            )
    if buyer_seat not in {0, 1}:
        raise RuntimeError(
            "ADX_REAL_RUNTIME_E2E_BUYER_SEAT must be -1, 0, or 1"
        )
    if MARKET_PROTOCOL not in {"fcfs.v1", "agent_a2a.v1"}:
        raise RuntimeError(
            "ADX_REAL_RUNTIME_E2E_MARKET_PROTOCOL must be fcfs.v1 or "
            "agent_a2a.v1"
        )

    run_id = uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix="arena402-real-runtimes-") as temporary:
        temp_root = Path(temporary)
        connector_executable = temp_root / (
            "adx-connector-e2e.exe" if os.name == "nt" else "adx-connector-e2e"
        )
        _build_connector(connector_executable)
        codex_shim_root = temp_root / "codex-shim"
        if "codex" in RUNTIME_KINDS:
            _create_codex_shim(codex_shim_root)

        users = tuple(
            await asyncio.gather(
                *(
                    _create_user(
                        invites[index],
                        f"seat{index + 1}_{runtime_kind.replace('-', '_')}",
                        run_id,
                    )
                    for index, runtime_kind in enumerate(RUNTIME_KINDS)
                )
            )
        )
        credentials = tuple(
            await asyncio.gather(
                *(
                    _create_device_credential(
                        users[index],
                        device_name=(
                            f"Real {runtime_kind} E2E seat {index + 1}"
                        ),
                    )
                    for index, runtime_kind in enumerate(RUNTIME_KINDS)
                )
            )
        )
        connectors = tuple(
            RealConnector(
                kind=runtime_kind,
                label=f"seat-{index + 1}",
                user=users[index],
                credential=credentials[index],
                connector_executable=connector_executable,
                temp_root=temp_root,
                codex_shim_root=codex_shim_root,
                run_id=run_id,
            )
            for index, runtime_kind in enumerate(RUNTIME_KINDS)
        )
        game_id = f"real-runtimes-{run_id}"
        try:
            for connector in connectors:
                connector.start()
            bindings = tuple(
                await asyncio.gather(
                    *(connector.wait_online_and_bind() for connector in connectors)
                )
            )
            created = _require_ok(
                await users[0].client.post(
                    "/api/v1/pawnhouse/games",
                    headers=users[0].mutation_headers,
                    json={
                        "gameId": game_id,
                        "eventSeed": EVENT_SEED,
                        "actionTimeoutMs": ACTION_TIMEOUT_MS,
                        "roundCount": ROUND_COUNT,
                        "eventMode": "seeded_shuffle",
                        "marketProtocol": MARKET_PROTOCOL,
                        "maxParticipants": 2,
                        "portfolioMode": "manual",
                        "settlement": {"authorizationMode": "none"},
                    },
                )
            )
            join_responses = await asyncio.gather(
                *(
                    users[index].client.post(
                        f"/api/v1/pawnhouse/games/{game_id}/connector-participants",
                        headers=users[index].mutation_headers,
                        json={
                            "agentId": bindings[index]["agent_id"],
                            "portfolio": (
                                {"cash": "20.000000", "holdings": {}}
                                if SAFE_NO_TRADE or index == buyer_seat
                                else {
                                    "cash": "0.000000",
                                    "holdings": {"grain": 10},
                                }
                            ),
                        },
                    )
                    for index in range(2)
                )
            )
            for response in join_responses:
                _require_ok(response)
            started = _require_ok(
                await users[0].client.post(
                    f"/api/v1/pawnhouse/games/{game_id}/start",
                    headers=users[0].mutation_headers,
                )
            )
            final_state = await _wait_for_completion(
                users[0],
                game_id,
                connectors,
            )
            evidence = (
                await _public_evidence(users[0], game_id)
                if PUBLIC_EVIDENCE_ONLY
                else await _database_evidence(game_id)
            )
            print(
                json.dumps(
                    {
                        "gameId": game_id,
                        "createdPhase": created["phase"],
                        "startedPhase": started["phase"],
                        "finalPhase": final_state["phase"],
                        "runtimes": {
                            connector.label: {
                                "kind": connector.kind,
                                "version": connector.runtime["version"],
                                "runtimeId": connector.runtime["runtime_id"],
                                "isolation": connector.runtime["arena_isolation"],
                                **(
                                    {"serviceTierOverride": "fast"}
                                    if connector.kind == "codex"
                                    else {}
                                ),
                            }
                            for connector in connectors
                        },
                        "taskTransport": "mcp",
                        "eventSeed": EVENT_SEED,
                        "roundCount": ROUND_COUNT,
                        "marketProtocol": MARKET_PROTOCOL,
                        "safeNoTrade": SAFE_NO_TRADE,
                        "expectedMatch": EXPECT_MATCH,
                        "expectedDeal": EXPECT_DEAL,
                        "buyerRuntime": RUNTIME_KINDS[buyer_seat],
                        "buyerSeat": connectors[buyer_seat].label,
                        "evidenceClass": "real_local_connector_agents",
                        "evidence": evidence,
                        "chainWrites": 0,
                    },
                    separators=(",", ":"),
                    default=str,
                )
            )
        except Exception as exc:
            connector_logs = "\n\n".join(
                (
                    f"[{connector.label}:{connector.kind}]\n"
                    f"{connector._failure_logs()}"
                )
                for connector in connectors
            )
            raise RuntimeError(
                f"{exc}\nConnector logs:\n{connector_logs}"
            ) from exc
        finally:
            await asyncio.gather(*(connector.stop() for connector in connectors))
            await asyncio.gather(*(user.client.aclose() for user in users))


if __name__ == "__main__":
    asyncio.run(main())
