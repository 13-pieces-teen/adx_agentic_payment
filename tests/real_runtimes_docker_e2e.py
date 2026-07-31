"""Real Claude Code + Codex game through Docker Arena and stateless MCP.

Prerequisites:

* an isolated Compose stack using ``docker-compose.real-runtimes-e2e.yml``;
* two one-time invites in ``ADX_REAL_RUNTIME_E2E_INVITES`` as a JSON array;
* locally authenticated ``claude`` and ``codex`` CLIs;
* Go available to build the current Connector source.

Arena, Gateway, PostgreSQL, and the Arena worker run in Docker. The two
Connectors and their managed Runtime children run on the host so existing local
CLI authentication never enters a container. Payment support is disabled and
the game freezes ``authorizationMode=none``.
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
EVENT_SEED = "real-runtime-grain-1"


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
        user: UserSession,
        credential: dict[str, str],
        connector_executable: Path,
        temp_root: Path,
        codex_shim_root: Path,
        run_id: str,
    ) -> None:
        self.kind = kind
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
        connector_state_root = self.temp_root / self.kind
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
                    f"{self.kind} Connector exited before readiness:\n"
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
                                        "Real Claude Code"
                                        if self.kind == "claude-code"
                                        else "Real Codex"
                                    ),
                                    "working_directory": str(ROOT),
                                },
                            )
                        )
                        registration = self.binding.get("arenaRegistration", {})
                        if registration.get("routeStatus") != "ready":
                            raise RuntimeError(
                                f"{self.kind} Arena route is not ready: "
                                f"{registration!r}"
                            )
                        return self.binding
            await asyncio.sleep(0.25)
        raise RuntimeError(
            f"{self.kind} Connector did not publish a locally ready Runtime:\n"
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
        last_state = _require_ok(
            await user.client.get(f"/api/v1/pawnhouse/games/{game_id}")
        )
        if last_state.get("phase") == "completed":
            return last_state
        for connector in connectors:
            assert connector.process is not None
            exit_code = connector.process.poll()
            if exit_code is not None:
                raise RuntimeError(
                    f"{connector.kind} Connector exited with {exit_code}:\n"
                    f"{connector._failure_logs()}"
                )
        await asyncio.sleep(0.5)
    logs = "\n\n".join(
        f"[{connector.kind}]\n{connector._failure_logs()}" for connector in connectors
    )
    raise RuntimeError(
        f"game did not complete before timeout; last state={last_state!r}\n" f"{logs}"
    )


async def _database_evidence(game_id: str) -> dict[str, Any]:
    connection = await asyncpg.connect(ADMIN_URL)
    try:
        game = await connection.fetchrow(
            """
            SELECT phase, current_round, round_count, config_snapshot
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
        counts = await connection.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM arena402.pairings
                 WHERE game_id = $1) AS pairings,
                (SELECT count(*) FROM arena402.negotiation_messages
                 WHERE game_id = $1) AS negotiation_messages,
                (SELECT count(*) FROM arena402.settlement_intents
                 WHERE game_id = $1) AS settlement_intents,
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
    decide_tasks = [row for row in task_values if row["kind"] == "arena.decide"]
    if len(decide_tasks) != 2:
        raise RuntimeError(f"expected two real decide tasks: {task_values!r}")
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

    config = game["config_snapshot"]
    if isinstance(config, str):
        config = json.loads(config)
    return {
        "game": {
            "phase": game["phase"],
            "currentRound": int(game["current_round"]),
            "roundCount": int(game["round_count"]),
            "authorizationMode": dict(config)
            .get("settlement", {})
            .get("authorizationMode"),
        },
        "participants": participant_values,
        "tasks": task_values,
        "decisions": [row["public_payload"] for row in decisions],
        "negotiations": [dict(row) for row in negotiation_rows],
        "counts": dict(counts),
    }


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
        raise RuntimeError(f"Arena MCP is not enabled: {health.json()!r}")

    run_id = uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix="arena402-real-runtimes-") as temporary:
        temp_root = Path(temporary)
        connector_executable = temp_root / (
            "adx-connector-e2e.exe" if os.name == "nt" else "adx-connector-e2e"
        )
        _build_connector(connector_executable)
        codex_shim_root = temp_root / "codex-shim"
        _create_codex_shim(codex_shim_root)

        claude_user, codex_user = await asyncio.gather(
            _create_user(invites[0], "claude", run_id),
            _create_user(invites[1], "codex", run_id),
        )
        claude_credential, codex_credential = await asyncio.gather(
            _create_device_credential(
                claude_user,
                device_name="Real Claude Code E2E",
            ),
            _create_device_credential(
                codex_user,
                device_name="Real Codex E2E",
            ),
        )
        claude = RealConnector(
            kind="claude-code",
            user=claude_user,
            credential=claude_credential,
            connector_executable=connector_executable,
            temp_root=temp_root,
            codex_shim_root=codex_shim_root,
            run_id=run_id,
        )
        codex = RealConnector(
            kind="codex",
            user=codex_user,
            credential=codex_credential,
            connector_executable=connector_executable,
            temp_root=temp_root,
            codex_shim_root=codex_shim_root,
            run_id=run_id,
        )
        game_id = f"real-runtimes-{run_id}"
        try:
            claude.start()
            codex.start()
            claude_binding, codex_binding = await asyncio.gather(
                claude.wait_online_and_bind(),
                codex.wait_online_and_bind(),
            )
            created = _require_ok(
                await claude_user.client.post(
                    "/api/v1/pawnhouse/games",
                    headers=claude_user.mutation_headers,
                    json={
                        "gameId": game_id,
                        "eventSeed": EVENT_SEED,
                        "actionTimeoutMs": ACTION_TIMEOUT_MS,
                        "roundCount": 1,
                        "eventMode": "seeded_shuffle",
                        "maxParticipants": 2,
                        "portfolioMode": "manual",
                        "settlement": {"authorizationMode": "none"},
                    },
                )
            )
            join_responses = await asyncio.gather(
                claude_user.client.post(
                    f"/api/v1/pawnhouse/games/{game_id}/connector-participants",
                    headers=claude_user.mutation_headers,
                    json={
                        "agentId": claude_binding["agent_id"],
                        "portfolio": {"cash": "20.000000", "holdings": {}},
                    },
                ),
                codex_user.client.post(
                    f"/api/v1/pawnhouse/games/{game_id}/connector-participants",
                    headers=codex_user.mutation_headers,
                    json={
                        "agentId": codex_binding["agent_id"],
                        "portfolio": {
                            "cash": "0.000000",
                            "holdings": {"grain": 10},
                        },
                    },
                ),
            )
            for response in join_responses:
                _require_ok(response)
            started = _require_ok(
                await claude_user.client.post(
                    f"/api/v1/pawnhouse/games/{game_id}/start",
                    headers=claude_user.mutation_headers,
                )
            )
            final_state = await _wait_for_completion(
                claude_user,
                game_id,
                (claude, codex),
            )
            evidence = await _database_evidence(game_id)
            print(
                json.dumps(
                    {
                        "gameId": game_id,
                        "createdPhase": created["phase"],
                        "startedPhase": started["phase"],
                        "finalPhase": final_state["phase"],
                        "runtimes": {
                            "claudeCode": {
                                "version": claude.runtime["version"],
                                "runtimeId": claude.runtime["runtime_id"],
                                "isolation": claude.runtime["arena_isolation"],
                            },
                            "codex": {
                                "version": codex.runtime["version"],
                                "runtimeId": codex.runtime["runtime_id"],
                                "isolation": codex.runtime["arena_isolation"],
                                "serviceTierOverride": "fast",
                            },
                        },
                        "taskTransport": "mcp",
                        "eventSeed": EVENT_SEED,
                        "evidence": evidence,
                        "chainWrites": 0,
                    },
                    separators=(",", ":"),
                    default=str,
                )
            )
        finally:
            await asyncio.gather(claude.stop(), codex.stop())
            await asyncio.gather(
                claude_user.client.aclose(),
                codex_user.client.aclose(),
            )


if __name__ == "__main__":
    asyncio.run(main())
