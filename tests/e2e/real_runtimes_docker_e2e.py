"""Real local Runtime game through Docker Arena and stateless MCP.

Prerequisites:

* an isolated Compose stack using
  ``tests/e2e/docker-compose.real-runtimes-e2e.yml``;
* 2–100 one-time invites in ``ADX_REAL_RUNTIME_E2E_INVITES`` as a JSON array;
* locally authenticated CLIs selected by ``ADX_REAL_RUNTIME_E2E_RUNTIME_KINDS``;
* Go available to build the current Connector source.

Arena, Gateway, PostgreSQL, and the Arena worker run in Docker. Independent
Connectors and their managed Runtime children run on the host so existing local
CLI authentication never enters a container. The default remains one Claude
Code and one Codex Runtime for compatibility. A repeated Codex list plus
``ADX_REAL_RUNTIME_E2E_BUYER_SEATS`` runs a Codex-only load wave with separate
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


ROOT = Path(__file__).resolve().parents[2]
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
BUYER_RUNTIME = (
    os.getenv(
        "ADX_REAL_RUNTIME_E2E_BUYER_RUNTIME",
        "claude-code",
    )
    .strip()
    .lower()
)
RUNTIME_KINDS = tuple(
    value.strip().lower()
    for value in os.getenv(
        "ADX_REAL_RUNTIME_E2E_RUNTIME_KINDS",
        "claude-code,codex",
    ).split(",")
    if value.strip()
)
BUYER_SEAT = int(os.getenv("ADX_REAL_RUNTIME_E2E_BUYER_SEAT", "-1"))
BUYER_SEATS = os.getenv(
    "ADX_REAL_RUNTIME_E2E_BUYER_SEATS",
    "",
).strip()
SELLER_GOODS = tuple(
    value.strip().lower()
    for value in os.getenv(
        "ADX_REAL_RUNTIME_E2E_SELLER_GOODS",
        "",
    ).split(",")
    if value.strip()
)
MARKET_PROTOCOL = os.getenv(
    "ADX_REAL_RUNTIME_E2E_MARKET_PROTOCOL",
    "fcfs.v1",
).strip()


@dataclass(frozen=True, slots=True)
class E2ETopology:
    invites: tuple[str, ...]
    runtime_kinds: tuple[str, ...]
    buyer_seats: frozenset[int]

    @property
    def participant_count(self) -> int:
        return len(self.invites)


def resolve_topology(
    *,
    invites: list[str] | tuple[str, ...],
    runtime_kinds: list[str] | tuple[str, ...],
    buyer_seats: range | list[int] | tuple[int, ...] | frozenset[int],
) -> E2ETopology:
    normalized_invites = tuple(value.strip() for value in invites)
    normalized_kinds = tuple(value.strip().lower() for value in runtime_kinds)
    normalized_buyers = frozenset(buyer_seats)
    participant_count = len(normalized_invites)
    if participant_count < 2 or participant_count > 100:
        raise ValueError("real Runtime E2E requires between 2 and 100 participants")
    if len(normalized_kinds) != participant_count:
        raise ValueError("invite and Runtime-kind counts must match")
    if any(not value for value in normalized_invites):
        raise ValueError("invites must be non-empty")
    if len(set(normalized_invites)) != participant_count:
        raise ValueError("invites must be unique")
    if any(kind not in {"claude-code", "codex"} for kind in normalized_kinds):
        raise ValueError("unsupported real Runtime kind")
    if (
        not normalized_buyers
        or len(normalized_buyers) == participant_count
        or any(
            isinstance(seat, bool)
            or not isinstance(seat, int)
            or seat < 0
            or seat >= participant_count
            for seat in normalized_buyers
        )
    ):
        raise ValueError("buyer seats must leave at least one buyer and seller")
    return E2ETopology(
        invites=normalized_invites,
        runtime_kinds=normalized_kinds,
        buyer_seats=normalized_buyers,
    )


def portfolio_for_seat(
    seat: int,
    buyer_seats: frozenset[int],
    *,
    safe_no_trade: bool = False,
    seller_good: str = "grain",
) -> dict[str, object]:
    if safe_no_trade or seat in buyer_seats:
        return {"cash": "20.000000", "holdings": {}}
    seller_portfolios = {
        "grain": {"cash": "0.000000", "holdings": {"grain": 10}},
        "iron": {"cash": "0.000000", "holdings": {"iron": 4}},
        "warhorse": {"cash": "4.000000", "holdings": {"warhorse": 2}},
        "gems": {"cash": "2.000000", "holdings": {"gems": 6}},
    }
    try:
        return seller_portfolios[seller_good]
    except KeyError as exc:
        raise ValueError(f"unsupported seller good: {seller_good}") from exc


def game_create_payload(
    *,
    game_id: str,
    event_seed: str,
    participant_count: int,
    action_timeout_ms: int,
    round_count: int,
    market_protocol: str,
) -> dict[str, object]:
    return {
        "gameId": game_id,
        "eventSeed": event_seed,
        "actionTimeoutMs": action_timeout_ms,
        "roundCount": round_count,
        "eventMode": "seeded_shuffle",
        "marketProtocol": market_protocol,
        "maxParticipants": participant_count,
        "portfolioMode": "manual",
        "settlement": {"authorizationMode": "none"},
    }


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
        gateway_url: str = WS_URL,
    ) -> None:
        self.kind = kind
        self.label = label
        self.user = user
        self.credential = credential
        self.connector_executable = connector_executable
        self.temp_root = temp_root
        self.codex_shim_root = codex_shim_root
        self.run_id = run_id
        self.gateway_url = gateway_url
        self.process: subprocess.Popen[str] | None = None
        self.logs: deque[str] = deque(maxlen=400)
        self.runtime: dict[str, Any] = {}
        self.binding: dict[str, Any] = {}

    @property
    def state_path(self) -> Path:
        return self.temp_root / self.label / f"state-{self.run_id}.json"

    def start(self) -> None:
        environment = os.environ.copy()
        environment["ADX_CONNECTOR_DEVICE_ID"] = self.credential["device_id"]
        environment["ADX_CONNECTOR_TOKEN"] = self.credential["device_token"]
        environment["ADX_CONNECTOR_GATEWAY_URL"] = self.gateway_url
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
            self.gateway_url,
            "--task-transport",
            "mcp",
            "--auto-pair=false",
            "--state",
            str(self.state_path),
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
        arguments.extend(
            [
                "--runtime-kind",
                "codex" if self.kind == "codex" else "claude_code",
            ]
        )
        process = subprocess.Popen(
            arguments,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.process = process
        assert process.stdout is not None

        def drain() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                self.logs.append(line.rstrip())

        threading.Thread(target=drain, daemon=True).start()

    def _failure_logs(self) -> str:
        return "\n".join(list(self.logs)[-100:])

    async def wait_online(self) -> dict[str, Any]:
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
                        return runtime
            await asyncio.sleep(0.25)
        raise RuntimeError(
            f"{self.label} ({self.kind}) Connector did not publish a locally "
            "ready Runtime:\n"
            f"{self._failure_logs()}"
        )

    async def wait_online_and_bind(self) -> dict[str, Any]:
        runtime = await self.wait_online()
        device_id = self.credential["device_id"]
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


async def _database_evidence(
    game_id: str,
    *,
    expected_participants: int | None = None,
) -> dict[str, Any]:
    expected_participants = expected_participants or len(RUNTIME_KINDS)
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
                    FROM arena402.market_intents
                    WHERE game_id = $1
                      AND status IN ('open', 'reserved')
                ) AS nonterminal_market_intents,
                (
                    SELECT count(*)
                    FROM arena402.market_negotiation_requests
                    WHERE game_id = $1
                      AND status = 'pending'
                ) AS pending_market_requests,
                (
                    SELECT count(*)
                    FROM arena402.market_rfq_sessions
                    WHERE game_id = $1
                      AND status = 'active'
                ) AS active_market_sessions,
                (
                    SELECT count(*)
                    FROM arena402.participant_round_slots
                    WHERE game_id = $1
                      AND status = 'reserved'
                ) AS reserved_market_slots,
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
    if len(participant_values) != expected_participants:
        raise RuntimeError(
            f"expected {expected_participants} Connector participants: "
            f"{participant_values!r}"
        )
    if int(counts["rankings"]) != expected_participants:
        raise RuntimeError(
            f"expected {expected_participants} rankings: {dict(counts)!r}"
        )
    opening_kind = (
        "arena.market.intent" if MARKET_PROTOCOL == "agent_a2a.v1" else "arena.decide"
    )
    opening_tasks = [row for row in task_values if row["kind"] == opening_kind]
    expected_opening_tasks = expected_participants * ROUND_COUNT
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
            "no-payment E2E moved authoritative inventory: " f"{dict(counts)!r}"
        )
    if MARKET_PROTOCOL == "agent_a2a.v1":
        assert_terminal_agent_market(dict(counts))
    if EXPECT_MATCH and (
        int(counts["pairings"]) < 1 or int(counts["negotiation_messages"]) < 1
    ):
        raise RuntimeError(f"trade probe did not reach negotiation: {dict(counts)!r}")
    deal_values = [dict(row) for row in deal_rows]
    for deal in deal_values:
        if (
            deal["latest_proposal_result_id"] == deal["acceptance_result_id"]
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


def assert_terminal_agent_market(counts: dict[str, Any]) -> None:
    residual = {
        key: int(counts[key])
        for key in (
            "nonterminal_market_intents",
            "pending_market_requests",
            "active_market_sessions",
            "reserved_market_slots",
        )
        if int(counts[key]) != 0
    }
    if residual:
        raise RuntimeError(
            "completed A2A game retained nonterminal market state: "
            f"{residual!r}"
        )


async def _public_evidence(
    user: UserSession,
    game_id: str,
    *,
    expected_participants: int | None = None,
) -> dict[str, Any]:
    expected_participants = expected_participants or len(RUNTIME_KINDS)
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
    if len(state.get("participants", [])) != expected_participants:
        raise RuntimeError(
            f"expected {expected_participants} public participants: {state!r}"
        )
    if len(state.get("rankings", [])) != expected_participants:
        raise RuntimeError(
            f"expected {expected_participants} public rankings: {state!r}"
        )
    if runtime_run.get("status") != "completed":
        raise RuntimeError(f"public Runtime Run is not completed: {runtime_run!r}")
    if SAFE_NO_TRADE and settlements.get("total") != 0:
        raise RuntimeError(
            f"safe no-trade game created a settlement intent: {settlements!r}"
        )
    if EXPECT_MATCH and not state.get("pairings"):
        raise RuntimeError(f"public trade probe did not create a pairing: {state!r}")
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
    if not isinstance(invites, list) or not all(
        isinstance(value, str) for value in invites
    ):
        raise RuntimeError(
            "ADX_REAL_RUNTIME_E2E_INVITES must be a JSON array of invites"
        )
    health = httpx.get(f"{API_BASE}/api/health", timeout=15)
    health.raise_for_status()
    if health.json().get("arena_mcp") is not True:
        _probe_mcp_data_plane()
    if ROUND_COUNT < 1 or ROUND_COUNT > 10:
        raise RuntimeError("ADX_REAL_RUNTIME_E2E_ROUND_COUNT must be between 1 and 10")
    if BUYER_SEATS:
        try:
            buyer_seats = tuple(
                int(value.strip()) for value in BUYER_SEATS.split(",") if value.strip()
            )
        except ValueError as exc:
            raise RuntimeError(
                "ADX_REAL_RUNTIME_E2E_BUYER_SEATS must contain integer seats"
            ) from exc
    else:
        buyer_seat = BUYER_SEAT
        if buyer_seat != -1:
            buyer_seats = (buyer_seat,)
        else:
            matches = [
                index
                for index, runtime_kind in enumerate(RUNTIME_KINDS)
                if runtime_kind == BUYER_RUNTIME
            ]
            if len(matches) == 1:
                buyer_seats = (matches[0],)
            elif len(set(RUNTIME_KINDS)) == 1:
                buyer_seats = (0,)
            else:
                raise RuntimeError(
                    "ADX_REAL_RUNTIME_E2E_BUYER_RUNTIME must identify one "
                    "Runtime or ADX_REAL_RUNTIME_E2E_BUYER_SEATS must be set"
                )
    try:
        topology = resolve_topology(
            invites=invites,
            runtime_kinds=RUNTIME_KINDS,
            buyer_seats=buyer_seats,
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    participant_count = topology.participant_count
    runtime_kinds = topology.runtime_kinds
    buyer_seats = topology.buyer_seats
    seller_seats = sorted(set(range(participant_count)) - buyer_seats)
    seller_goods = SELLER_GOODS or ("grain",) * len(seller_seats)
    if len(seller_goods) != len(seller_seats):
        raise RuntimeError(
            "ADX_REAL_RUNTIME_E2E_SELLER_GOODS must contain exactly one "
            "good for each seller seat"
        )
    seller_goods_by_seat = dict(zip(seller_seats, seller_goods, strict=True))
    try:
        for seat in seller_seats:
            portfolio_for_seat(
                seat,
                buyer_seats,
                seller_good=seller_goods_by_seat[seat],
            )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if MARKET_PROTOCOL not in {"fcfs.v1", "agent_a2a.v1"}:
        raise RuntimeError(
            "ADX_REAL_RUNTIME_E2E_MARKET_PROTOCOL must be fcfs.v1 or " "agent_a2a.v1"
        )

    run_id = uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix="arena402-real-runtimes-") as temporary:
        temp_root = Path(temporary)
        connector_executable = temp_root / (
            "adx-connector-e2e.exe" if os.name == "nt" else "adx-connector-e2e"
        )
        _build_connector(connector_executable)
        codex_shim_root = temp_root / "codex-shim"
        if "codex" in runtime_kinds:
            _create_codex_shim(codex_shim_root)

        users = tuple(
            await asyncio.gather(
                *(
                    _create_user(
                        invites[index],
                        f"seat{index + 1}_{runtime_kind.replace('-', '_')}",
                        run_id,
                    )
                    for index, runtime_kind in enumerate(runtime_kinds)
                )
            )
        )
        credentials = tuple(
            await asyncio.gather(
                *(
                    _create_device_credential(
                        users[index],
                        device_name=(f"Real {runtime_kind} E2E seat {index + 1}"),
                    )
                    for index, runtime_kind in enumerate(runtime_kinds)
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
            for index, runtime_kind in enumerate(runtime_kinds)
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
                    json=game_create_payload(
                        game_id=game_id,
                        event_seed=EVENT_SEED,
                        participant_count=participant_count,
                        action_timeout_ms=ACTION_TIMEOUT_MS,
                        round_count=ROUND_COUNT,
                        market_protocol=MARKET_PROTOCOL,
                    ),
                )
            )
            join_responses = await asyncio.gather(
                *(
                    users[index].client.post(
                        f"/api/v1/pawnhouse/games/{game_id}/connector-participants",
                        headers=users[index].mutation_headers,
                        json={
                            "agentId": bindings[index]["agent_id"],
                            "portfolio": portfolio_for_seat(
                                index,
                                buyer_seats,
                                safe_no_trade=SAFE_NO_TRADE,
                                seller_good=seller_goods_by_seat.get(
                                    index,
                                    "grain",
                                ),
                            ),
                        },
                    )
                    for index in range(participant_count)
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
                await _public_evidence(
                    users[0],
                    game_id,
                    expected_participants=participant_count,
                )
                if PUBLIC_EVIDENCE_ONLY
                else await _database_evidence(
                    game_id,
                    expected_participants=participant_count,
                )
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
                        "buyerRuntimes": [
                            runtime_kinds[index] for index in sorted(buyer_seats)
                        ],
                        "buyerSeats": [
                            connectors[index].label for index in sorted(buyer_seats)
                        ],
                        **(
                            {
                                "buyerRuntime": runtime_kinds[next(iter(buyer_seats))],
                                "buyerSeat": connectors[next(iter(buyer_seats))].label,
                            }
                            if len(buyer_seats) == 1
                            else {}
                        ),
                        "sellerGoods": {
                            connectors[index].label: seller_goods_by_seat[index]
                            for index in seller_seats
                        },
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
            raise RuntimeError(f"{exc}\nConnector logs:\n{connector_logs}") from exc
        finally:
            await asyncio.gather(*(connector.stop() for connector in connectors))
            await asyncio.gather(*(user.client.aclose() for user in users))


if __name__ == "__main__":
    asyncio.run(main())
