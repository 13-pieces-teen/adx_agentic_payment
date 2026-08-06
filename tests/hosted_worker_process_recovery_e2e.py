"""External-process recovery E2E for the production Hosted Worker.

The default command orchestrates disposable Docker resources:

* a fresh PostgreSQL database with every approved migration;
* the production ``hosted_agent_runtime.production_worker`` entrypoint;
* a controllable, network-local LiteLLM protocol double;
* one crash before an Attempt exists and one crash after ``request_sent``.

The script deliberately uses SIGKILL for the crashed workers.  It never enables
Arena payments and never contacts a real model provider.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = (
    "postgres:17-alpine3.22@sha256:"
    "b02d9b5bcf608c2719da32cdabee274a33841202487fd5dc9b065b63f886753f"
)
DATABASE_NAME = "arena402_recovery"
ADMIN_PASSWORD = "arena402-recovery-admin"
API_PASSWORD = "arena402-recovery-api"
WORKER_PASSWORD = "arena402-recovery-worker"
CORE_PASSWORD = "arena402-recovery-core"
OTHER_ROLE_PASSWORD = "arena402-recovery-other-role"
MASTER_KEY_FILE = "/run/secrets/arena402/hosted-master.key"


class RecoveryE2EError(RuntimeError):
    """One externally observable recovery invariant failed."""


def _command(
    arguments: Sequence[str],
    *,
    timeout: float = 180,
    capture: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(arguments),
        cwd=ROOT,
        check=False,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        raise RecoveryE2EError(
            "command failed "
            f"({completed.returncode}): {' '.join(arguments)}\n"
            f"stdout:\n{stdout[-4000:]}\n"
            f"stderr:\n{stderr[-4000:]}"
        )
    return completed


def _docker(
    *arguments: str,
    timeout: float = 180,
    capture: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _command(
        ("docker", *arguments),
        timeout=timeout,
        capture=capture,
        check=check,
    )


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind(("127.0.0.1", 0))
        return int(stream.getsockname()[1])


def _wait_until(
    description: str,
    predicate: Callable[[], bool],
    *,
    timeout_seconds: float = 30,
    interval_seconds: float = 0.1,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as error:  # pragma: no cover - diagnostic path
            last_error = error
        time.sleep(interval_seconds)
    suffix = f": {last_error}" if last_error is not None else ""
    raise RecoveryE2EError(f"timed out waiting for {description}{suffix}")


def _http_json(
    port: int,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    encoded = (
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if payload is not None
        else None
    )
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=encoded,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            value = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RecoveryE2EError(f"fake provider control failed: {error}") from error
    if not isinstance(value, dict):
        raise RecoveryE2EError("fake provider returned an invalid control body")
    return value


class _FakeProviderState:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.mode = "respond"
        self.validation_requests = 0
        self.model_requests = 0
        self.active_requests = 0

    def snapshot(self) -> dict[str, object]:
        with self.condition:
            return {
                "mode": self.mode,
                "validationRequests": self.validation_requests,
                "modelRequests": self.model_requests,
                "activeRequests": self.active_requests,
            }

    def reset(self) -> None:
        with self.condition:
            self.validation_requests = 0
            self.model_requests = 0
            self.active_requests = 0

    def set_mode(self, value: str) -> None:
        if value not in {"respond", "block"}:
            raise ValueError("invalid fake provider mode")
        with self.condition:
            self.mode = value
            self.condition.notify_all()

    def wait_if_blocked(self) -> None:
        with self.condition:
            while self.mode == "block":
                self.condition.wait(timeout=0.5)


def _chat_completion(body: dict[str, object]) -> tuple[dict[str, object], bool]:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return (
            {
                "id": f"chatcmpl-validation-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"ok":true}',
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            },
            True,
        )

    functions: dict[str, dict[str, object]] = {}
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str):
            functions[name] = function

    messages = body.get("messages")
    has_tool_result = isinstance(messages, list) and any(
        isinstance(message, dict) and message.get("role") == "tool"
        for message in messages
    )
    if not has_tool_result:
        tool_name = next(
            (
                name
                for name in functions
                if name == "recall_strategy_and_plan"
            ),
            None,
        )
        if tool_name is None:
            raise RecoveryE2EError(
                "PydanticAI request omitted recall_strategy_and_plan"
            )
        arguments: dict[str, object] = {}
    else:
        tool_name = None
        for name, function in functions.items():
            parameters = function.get("parameters")
            if not isinstance(parameters, dict):
                continue
            properties = parameters.get("properties")
            if not isinstance(properties, dict):
                continue
            if {
                "action",
                "decision_summary",
                "memory_patch",
            }.issubset(properties):
                tool_name = name
                break
        if tool_name is None:
            raise RecoveryE2EError(
                "PydanticAI request omitted the terminal output tool"
            )
        arguments = {
            "action": {"action": "pass"},
            "decision_summary": {
                "plan": "Preserve liquidity during the recovery probe.",
                "factors": ["The isolated probe has no trading objective."],
                "confidence_bps": 8000,
            },
            "memory_patch": {
                "round_summary": "Completed an isolated recovery decision.",
                "next_plan": "Continue using only authoritative Arena state.",
                "observations": ["The process recovery path remained bounded."],
                "strategy_adjustments": [],
                "risk_budget_bps": 4000,
            },
        }

    return (
        {
            "id": f"chatcmpl-recovery-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": f"call_{uuid.uuid4().hex[:12]}",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(
                                        arguments,
                                        separators=(",", ":"),
                                    ),
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        },
        False,
    )


def _run_fake_provider(port: int) -> None:
    state = _FakeProviderState()

    class Handler(BaseHTTPRequestHandler):
        server_version = "Arena402RecoveryProvider/1"

        def log_message(self, *_: object) -> None:
            return

        def _send(self, status: int, payload: dict[str, object]) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode(
                "utf-8"
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _read_json(self) -> dict[str, object]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                raise RecoveryE2EError("invalid fake provider request") from None
            if not isinstance(payload, dict):
                raise RecoveryE2EError("invalid fake provider request")
            return payload

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/control/status":
                self._send(200, state.snapshot())
                return
            self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/control/reset":
                state.reset()
                self._send(200, state.snapshot())
                return
            if self.path == "/control/mode":
                try:
                    value = self._read_json().get("mode")
                    if not isinstance(value, str):
                        raise ValueError("missing mode")
                    state.set_mode(value)
                except (RecoveryE2EError, ValueError):
                    self._send(400, {"error": "invalid_mode"})
                    return
                self._send(200, state.snapshot())
                return
            if self.path != "/v1/chat/completions":
                self._send(404, {"error": "not_found"})
                return

            try:
                body = self._read_json()
                response, validation = _chat_completion(body)
            except RecoveryE2EError as error:
                self._send(400, {"error": str(error)})
                return

            with state.condition:
                if validation:
                    state.validation_requests += 1
                else:
                    state.model_requests += 1
                state.active_requests += 1
            try:
                state.wait_if_blocked()
                self._send(200, response)
            finally:
                with state.condition:
                    state.active_requests = max(0, state.active_requests - 1)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever(poll_interval=0.1)


async def _internal_seed_agent(args: argparse.Namespace) -> None:
    import asyncpg

    from hosted_agent_control_plane import (
        CredentialIngressRequest,
        CredentialIngressService,
        HostedAgentCreateRequest,
        HostedAgentService,
        PostgresHostedAgentControlRepository,
    )
    from hosted_agent_runtime.encrypted_secret_store import (
        AesGcmSecretCipher,
        PostgresEncryptedSecretVault,
        PostgresEncryptedSecretWriter,
    )
    from hosted_agent_runtime.production_providers import (
        build_production_capability_registry,
    )

    owner_id = f"recovery-user-{args.suffix}"
    admin = await asyncpg.connect(args.admin_url)
    repository = PostgresHostedAgentControlRepository(args.api_url)
    writer = PostgresEncryptedSecretWriter(
        PostgresEncryptedSecretVault(
            args.api_url,
            role="adx_arena_api",
        ),
        AesGcmSecretCipher.from_file(MASTER_KEY_FILE),
    )
    try:
        await admin.execute(
            """
            INSERT INTO connector_users (
                user_id, username, password_hash, temporary
            )
            VALUES ($1, $2, 'recovery-only-hash', FALSE)
            """,
            owner_id,
            f"recovery_{args.suffix}",
        )
        await repository.initialize()
        await writer.initialize()
        credential_service = CredentialIngressService(
            repository,
            secret_writer=writer,
            fingerprint_pepper=b"r" * 32,
            fingerprint_pepper_version=1,
        )
        credential = await credential_service.create_credential(
            owner_user_id=owner_id,
            request=CredentialIngressRequest(
                provider_id="official-deepseek",
                api_key="recovery-gateway-token",
                idempotency_key=f"recovery-credential-{args.suffix}",
            ),
        )
        agent_service = HostedAgentService(
            repository,
            capabilities=build_production_capability_registry(
                include_official=True
            ),
            hosted_agents_enabled=True,
        )
        agent = await agent_service.create_hosted_agent(
            owner_user_id=owner_id,
            request=HostedAgentCreateRequest(
                display_name="External Recovery Agent",
                credential_id=credential.credential_id,
                provider_id="official-deepseek",
                model_id="deepseek-v4-flash",
                thinking_enabled=False,
                strategy_instructions=(
                    "Choose only bounded legal actions and preserve liquidity."
                ),
                idempotency_key=f"recovery-agent-{args.suffix}",
            ),
        )
        print(
            json.dumps(
                {
                    "ownerId": owner_id,
                    "agentId": agent.agent_id,
                    "credentialId": credential.credential_id,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            flush=True,
        )
    finally:
        await writer.close()
        await repository.close()
        await admin.close()


async def _internal_wait_agent(args: argparse.Namespace) -> None:
    from hosted_agent_control_plane import (
        HostedAgentService,
        PostgresHostedAgentControlRepository,
    )
    from hosted_agent_runtime.production_providers import (
        build_production_capability_registry,
    )

    repository = PostgresHostedAgentControlRepository(args.api_url)
    service = HostedAgentService(
        repository,
        capabilities=build_production_capability_registry(
            include_official=True
        ),
        hosted_agents_enabled=True,
    )
    deadline = time.monotonic() + args.timeout
    try:
        await repository.initialize()
        while time.monotonic() < deadline:
            agent = await service.get_hosted_agent(
                owner_user_id=args.owner_id,
                agent_id=args.agent_id,
            )
            if (
                agent.provisioning_status.value == "ready"
                and agent.route_status.value == "ready"
            ):
                print('{"status":"ready"}', flush=True)
                return
            if agent.provisioning_status.value in {"degraded", "disabled"}:
                raise RecoveryE2EError(
                    "external validation worker did not ready the Agent"
                )
            await asyncio.sleep(0.1)
        raise RecoveryE2EError("timed out waiting for the Hosted Agent")
    finally:
        await repository.close()


async def _internal_seed_game(args: argparse.Namespace) -> None:
    import asyncpg

    from arena_core import PostgresArenaParticipationRepository
    from arena_core.hashing import sha256_identifier, sha256_text_identifier

    game_id = f"recovery-game-{args.suffix}"
    admin = await asyncpg.connect(args.admin_url)
    participation = PostgresArenaParticipationRepository(args.api_url)
    try:
        await admin.execute(
            """
            INSERT INTO arena402.games (
                game_id,
                round_count,
                action_timeout_ms,
                min_participants,
                max_participants,
                config_snapshot,
                event_seed,
                event_schedule_commitment,
                market_protocol
            )
            VALUES (
                $1,
                10,
                90000,
                2,
                2,
                '{"initial_cash_atomic":1000000,'
                '"initial_inventory":{"grain":1,"iron":1,'
                '"warhorse":1,"gems":1},'
                '"marketProtocol":"fcfs.v1"}'::jsonb,
                'external-recovery-event-seed',
                $2,
                'fcfs.v1'
            )
            """,
            game_id,
            sha256_text_identifier(
                f"external-recovery-event-seed:{game_id}"
            ),
        )
        await admin.execute(
            """
            INSERT INTO arena402.game_goods (
                game_id,
                good_id,
                display_name,
                initial_price_atomic,
                price_decimal_places
            )
            SELECT $1, good_id, display_name, initial_price, 6
            FROM (
                VALUES
                    ('grain', 'Grain', 1000000::NUMERIC),
                    ('iron', 'Iron', 2000000::NUMERIC),
                    ('warhorse', 'Warhorse', 3000000::NUMERIC),
                    ('gems', 'Gems', 4000000::NUMERIC)
            ) AS goods(good_id, display_name, initial_price)
            """,
            game_id,
        )
        await admin.execute(
            """
            INSERT INTO games (
                game_id, status, action_timeout_ms, config_snapshot
            )
            VALUES (
                $1, 'open', 90000,
                '{"initial_cash_atomic":1000000,'
                '"initial_inventory":{"grain":1,"iron":1,'
                '"warhorse":1,"gems":1}}'::jsonb
            )
            """,
            game_id,
        )
        await participation.initialize()
        request_digest = sha256_identifier(
            {"agentId": args.agent_id, "gameId": game_id}
        )
        joined = await participation.join(
            owner_user_id=args.owner_id,
            game_id=game_id,
            agent_id=args.agent_id,
            key_digest=sha256_text_identifier(
                f"external-recovery-join:{args.suffix}"
            ),
            request_digest=request_digest,
        )
        print(
            json.dumps(
                {
                    "gameId": game_id,
                    "gameAgentId": joined.game_agent_id,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            flush=True,
        )
    finally:
        await participation.close()
        await admin.close()


async def _internal_seed_task(args: argparse.Namespace) -> None:
    import asyncpg

    from arena_agent_contracts import (
        AGENT_TASK_SCHEMA_VERSION_V1,
        ArenaAgentTaskV1,
    )
    from arena_core import PostgresArenaCoreRepository
    from arena_core.hashing import sha256_identifier
    from tests.arena_core_helpers import decide_input

    admin = await asyncpg.connect(args.admin_url)
    core = PostgresArenaCoreRepository(args.admin_url)
    task_id = f"recovery-task-{args.label}-{args.suffix}"
    round_id = f"{args.game_id}:recovery:{args.round_index}:{args.label}"
    deadline = datetime.now(timezone.utc) + timedelta(seconds=90)
    try:
        row = await admin.fetchrow(
            """
            SELECT config_snapshot, config_hash
            FROM game_agents
            WHERE game_agent_id = $1
            """,
            args.game_agent_id,
        )
        if row is None:
            raise RecoveryE2EError("recovery Game Agent is missing")
        config_value = row["config_snapshot"]
        config_snapshot = (
            json.loads(config_value)
            if isinstance(config_value, str)
            else dict(config_value)
        )
        await admin.execute(
            """
            INSERT INTO rounds (
                round_id,
                game_id,
                round_index,
                phase,
                deadline_at
            )
            VALUES ($1, $2, $3, 'decide', $4)
            """,
            round_id,
            args.game_id,
            args.round_index,
            deadline,
        )
        task_input = decide_input(deadline=deadline).model_copy(
            update={
                "game_id": args.game_id,
                "round_id": round_id,
                "round_index": args.round_index,
            }
        )
        task = ArenaAgentTaskV1(
            task_id=task_id,
            kind="arena.decide",
            schema_version=AGENT_TASK_SCHEMA_VERSION_V1,
            game_id=args.game_id,
            round_id=round_id,
            game_agent_id=args.game_agent_id,
            deadline_at=deadline,
            idempotency_key=(
                f"{args.game_id}:{round_id}:"
                f"{args.game_agent_id}:decide"
            ),
            input_hash=sha256_identifier(task_input),
            input=task_input,
        )
        await core.initialize()
        await core.create_task(
            task=task,
            config_snapshot=config_snapshot,
            config_hash=row["config_hash"],
            created_at=datetime.now(timezone.utc),
        )
        print(
            json.dumps({"taskId": task_id}, separators=(",", ":")),
            flush=True,
        )
    finally:
        await core.close()
        await admin.close()


async def _internal_snapshot(args: argparse.Namespace) -> None:
    import asyncpg

    admin = await asyncpg.connect(args.admin_url)
    try:
        task = await admin.fetchrow(
            """
            SELECT
                task_id,
                status,
                terminal_reason,
                leased_by,
                attempt_count,
                completed_at
            FROM arena_agent_tasks
            WHERE task_id = $1
            """,
            args.task_id,
        )
        attempts = await admin.fetch(
            """
            SELECT
                attempt_no,
                worker_id,
                status,
                request_sent_at,
                runtime_completed_at,
                error_class
            FROM arena_agent_task_attempts
            WHERE task_id = $1
            ORDER BY attempt_no
            """,
            args.task_id,
        )
        results = await admin.fetch(
            """
            SELECT
                result_id,
                runtime_status,
                apply_status,
                error_class
            FROM arena_agent_task_results
            WHERE task_id = $1
            ORDER BY result_received_at, result_id
            """,
            args.task_id,
        )
        claims = await admin.fetch(
            """
            SELECT
                safe_metadata ->> 'worker_id' AS worker_id,
                created_at
            FROM arena_agent_task_events
            WHERE task_id = $1
              AND event_type = 'task_claimed'
            ORDER BY created_at, event_id
            """,
            args.task_id,
        )

        def normalize(value: Any) -> Any:
            if isinstance(value, datetime):
                return value.isoformat()
            return value

        def record(row: Any) -> dict[str, object]:
            return {
                key: normalize(value)
                for key, value in dict(row).items()
            }

        print(
            json.dumps(
                {
                    "task": record(task) if task is not None else None,
                    "attempts": [record(row) for row in attempts],
                    "results": [record(row) for row in results],
                    "claims": [record(row) for row in claims],
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            flush=True,
        )
    finally:
        await admin.close()


def _last_json(output: str) -> dict[str, object]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RecoveryE2EError(f"command did not emit a JSON object:\n{output}")


def _worker_environment(
    *,
    worker_url: str,
    worker_id: str,
) -> list[str]:
    values = {
        "ADX_HOSTED_WORKER_DATABASE_URL": worker_url,
        "ADX_HOSTED_SECRET_BACKEND": "postgres_aesgcm",
        "ADX_HOSTED_CREDENTIAL_BACKEND_VERIFIED": "true",
        "ADX_HOSTED_MASTER_KEY_FILE": MASTER_KEY_FILE,
        "ADX_HOSTED_MASTER_KEY_VERSION": "1",
        "ADX_HOSTED_WORKER_ID": worker_id,
        "ADX_HOSTED_WORKER_LEASE_SECONDS": "30",
        "ADX_HOSTED_WORKER_POLL_SECONDS": "0.05",
        "ADX_HOSTED_WORKER_TASK_CONCURRENCY": "1",
    }
    arguments: list[str] = []
    for name, value in values.items():
        arguments.extend(("-e", f"{name}={value}"))
    return arguments


def _run_worker(
    *,
    name: str,
    network: str,
    key_volume: str,
    image: str,
    worker_url: str,
    worker_id: str,
) -> None:
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "--init",
        "--network",
        network,
        "--mount",
        f"source={key_volume},target=/run/secrets/arena402,readonly",
        *_worker_environment(worker_url=worker_url, worker_id=worker_id),
        image,
        "python",
        "-m",
        "hosted_agent_runtime.production_worker",
    )


def _controller(
    *,
    image: str,
    network: str,
    key_volume: str,
    arguments: Sequence[str],
    with_key: bool = False,
    timeout: float = 60,
) -> dict[str, object]:
    command = [
        "run",
        "--rm",
        "--network",
        network,
        "-v",
        f"{ROOT}:/workspace:ro",
        "-w",
        "/workspace",
        "-e",
        "PYTHONPATH=/workspace",
    ]
    if with_key:
        command.extend(
            (
                "--mount",
                f"source={key_volume},target=/run/secrets/arena402,readonly",
            )
        )
    command.extend(
        (
            "--entrypoint",
            "python",
            image,
            "tests/hosted_worker_process_recovery_e2e.py",
            *arguments,
        )
    )
    completed = _docker(*command, timeout=timeout)
    return _last_json(completed.stdout)


def _psql_scalar(
    postgres_name: str,
    sql: str,
) -> str:
    completed = _docker(
        "exec",
        "-e",
        f"PGPASSWORD={ADMIN_PASSWORD}",
        postgres_name,
        "psql",
        "-U",
        "postgres",
        "-d",
        DATABASE_NAME,
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
    )
    return completed.stdout.strip()


def _container_running(name: str) -> bool:
    completed = _docker(
        "inspect",
        "--format",
        "{{.State.Running}}",
        name,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def _worker_logs(name: str) -> str:
    completed = _docker("logs", "--tail", "100", name, check=False)
    return f"{completed.stdout}\n{completed.stderr}".strip()


def _orchestrate(args: argparse.Namespace) -> None:
    if shutil.which("docker") is None:
        raise RecoveryE2EError("docker is required")

    suffix = uuid.uuid4().hex[:10]
    prefix = f"arena402-hosted-recovery-{suffix}"
    network = f"{prefix}-net"
    key_volume = f"{prefix}-key"
    postgres_name = f"{prefix}-postgres"
    provider_name = f"{prefix}-official-litellm"
    validation_worker = f"{prefix}-validation"
    before_worker_a = f"{prefix}-before-a"
    before_worker_b = f"{prefix}-before-b"
    after_worker_a = f"{prefix}-after-a"
    after_worker_b = f"{prefix}-after-b"
    lock_name = f"{prefix}-context-lock"
    lock_application_name = f"recovery-context-lock-{suffix}"
    created_containers: list[str] = []
    image = args.image or f"arena402-hosted-recovery-e2e:{suffix}"
    built_image = args.image is None
    provider_port = _free_local_port()

    admin_url = (
        f"postgresql://postgres:{ADMIN_PASSWORD}"
        f"@postgres:5432/{DATABASE_NAME}"
    )
    api_url = (
        f"postgresql://adx_api_login:{API_PASSWORD}"
        f"@postgres:5432/{DATABASE_NAME}"
    )
    worker_url = (
        f"postgresql://adx_hosted_worker_login:{WORKER_PASSWORD}"
        f"@postgres:5432/{DATABASE_NAME}"
    )

    role_passwords = {
        "ADX_API_DATABASE_PASSWORD": API_PASSWORD,
        "ADX_HOSTED_WORKER_DATABASE_PASSWORD": WORKER_PASSWORD,
        "ADX_ARENA_CORE_DATABASE_PASSWORD": CORE_PASSWORD,
        "ADX_SETTLEMENT_DATABASE_PASSWORD": OTHER_ROLE_PASSWORD,
        "ADX_CREDENTIAL_CONTROLLER_DATABASE_PASSWORD": OTHER_ROLE_PASSWORD,
        "ADX_WALLET_SIGNER_DATABASE_PASSWORD": OTHER_ROLE_PASSWORD,
        "ADX_WALLET_IMPORTER_DATABASE_PASSWORD": OTHER_ROLE_PASSWORD,
    }

    def add_container(name: str) -> None:
        created_containers.append(name)

    try:
        if built_image:
            _docker(
                "build",
                "--quiet",
                "--file",
                "deploy/docker/Dockerfile.api",
                "--tag",
                image,
                ".",
                timeout=600,
            )
        _docker("network", "create", network)
        _docker("volume", "create", key_volume)
        _docker(
            "run",
            "--rm",
            "--user",
            "root",
            "--mount",
            f"source={key_volume},target=/secrets",
            "--entrypoint",
            "sh",
            image,
            "-c",
            (
                "head -c 32 /dev/urandom > /secrets/hosted-master.key "
                "&& chown 10001:10001 /secrets/hosted-master.key "
                "&& chmod 0400 /secrets/hosted-master.key"
            ),
        )
        _docker(
            "run",
            "-d",
            "--name",
            postgres_name,
            "--network",
            network,
            "--network-alias",
            "postgres",
            "-e",
            f"POSTGRES_DB={DATABASE_NAME}",
            "-e",
            "POSTGRES_USER=postgres",
            "-e",
            f"POSTGRES_PASSWORD={ADMIN_PASSWORD}",
            "--health-cmd",
            f"pg_isready -U postgres -d {DATABASE_NAME}",
            "--health-interval",
            "1s",
            "--health-timeout",
            "3s",
            "--health-retries",
            "30",
            POSTGRES_IMAGE,
        )
        add_container(postgres_name)
        _wait_until(
            "fresh PostgreSQL readiness",
            lambda: (
                _docker(
                    "inspect",
                    "--format",
                    "{{.State.Health.Status}}",
                    postgres_name,
                ).stdout.strip()
                == "healthy"
            ),
        )

        _docker(
            "run",
            "--rm",
            "--network",
            network,
            "-e",
            f"DATABASE_URL={admin_url}",
            "-e",
            f"ADX_CONNECTOR_DATABASE_URL={admin_url}",
            "-e",
            "ADX_CONNECTOR_MIGRATIONS_DIR=/app/db/migrations",
            image,
            "python",
            "/app/deploy/scripts/migrate.py",
            "--scope",
            "all",
            timeout=180,
        )
        provision_arguments = [
            "run",
            "--rm",
            "--network",
            network,
            "-e",
            f"ADX_DATABASE_ADMIN_URL={admin_url}",
        ]
        for name, value in role_passwords.items():
            provision_arguments.extend(("-e", f"{name}={value}"))
        provision_arguments.extend(
            (
                image,
                "python",
                "/app/deploy/scripts/provision_db_roles.py",
            )
        )
        _docker(*provision_arguments)

        _docker(
            "run",
            "-d",
            "--name",
            provider_name,
            "--network",
            network,
            "--network-alias",
            "official-litellm",
            "-p",
            f"127.0.0.1:{provider_port}:4000",
            "-v",
            f"{ROOT}:/workspace:ro",
            "--entrypoint",
            "python",
            image,
            "/workspace/tests/hosted_worker_process_recovery_e2e.py",
            "fake-provider",
            "--port",
            "4000",
        )
        add_container(provider_name)
        _wait_until(
            "fake LiteLLM readiness",
            lambda: _http_json(
                provider_port,
                "/control/status",
            ).get("mode")
            == "respond",
        )

        seeded = _controller(
            image=image,
            network=network,
            key_volume=key_volume,
            with_key=True,
            arguments=(
                "internal-seed-agent",
                "--suffix",
                suffix,
                "--admin-url",
                admin_url,
                "--api-url",
                api_url,
            ),
        )
        owner_id = str(seeded["ownerId"])
        agent_id = str(seeded["agentId"])

        _run_worker(
            name=validation_worker,
            network=network,
            key_volume=key_volume,
            image=image,
            worker_url=worker_url,
            worker_id=f"validation-worker-{suffix}",
        )
        add_container(validation_worker)
        _controller(
            image=image,
            network=network,
            key_volume=key_volume,
            arguments=(
                "internal-wait-agent",
                "--api-url",
                api_url,
                "--owner-id",
                owner_id,
                "--agent-id",
                agent_id,
                "--timeout",
                "30",
            ),
            timeout=45,
        )
        _docker("stop", "--time", "5", validation_worker)
        _http_json(provider_port, "/control/reset", method="POST", payload={})

        game = _controller(
            image=image,
            network=network,
            key_volume=key_volume,
            arguments=(
                "internal-seed-game",
                "--suffix",
                suffix,
                "--admin-url",
                admin_url,
                "--api-url",
                api_url,
                "--owner-id",
                owner_id,
                "--agent-id",
                agent_id,
            ),
        )
        game_id = str(game["gameId"])
        game_agent_id = str(game["gameAgentId"])

        before = _controller(
            image=image,
            network=network,
            key_volume=key_volume,
            arguments=(
                "internal-seed-task",
                "--suffix",
                suffix,
                "--label",
                "before-send",
                "--round-index",
                "21",
                "--admin-url",
                admin_url,
                "--game-id",
                game_id,
                "--game-agent-id",
                game_agent_id,
            ),
        )
        before_task_id = str(before["taskId"])

        _docker(
            "run",
            "-d",
            "--name",
            lock_name,
            "--network",
            network,
            "-e",
            f"PGPASSWORD={ADMIN_PASSWORD}",
            "-e",
            f"PGAPPNAME={lock_application_name}",
            POSTGRES_IMAGE,
            "psql",
            "-h",
            "postgres",
            "-U",
            "postgres",
            "-d",
            DATABASE_NAME,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            (
                "BEGIN; "
                "LOCK TABLE public.hosted_agent_game_memory "
                "IN ACCESS EXCLUSIVE MODE; "
                "SELECT pg_sleep(90); "
                "COMMIT;"
            ),
        )
        add_container(lock_name)
        _wait_until(
            "runtime-context table lock",
            lambda: _psql_scalar(
                postgres_name,
                """
                SELECT count(*)
                FROM pg_locks AS lock
                JOIN pg_class AS relation
                  ON relation.oid = lock.relation
                WHERE relation.relname = 'hosted_agent_game_memory'
                  AND lock.mode = 'AccessExclusiveLock'
                  AND lock.granted
                """,
            )
            == "1",
        )

        before_id_a = f"external-before-a-{suffix}"
        before_id_b = f"external-before-b-{suffix}"
        _run_worker(
            name=before_worker_a,
            network=network,
            key_volume=key_volume,
            image=image,
            worker_url=worker_url,
            worker_id=before_id_a,
        )
        add_container(before_worker_a)
        _wait_until(
            "pre-send task claim without an Attempt",
            lambda: _psql_scalar(
                postgres_name,
                f"""
                SELECT (
                    task.status = 'leased'
                    AND task.leased_by = '{before_id_a}'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM arena_agent_task_attempts AS attempt
                        WHERE attempt.task_id = task.task_id
                    )
                )::text
                FROM arena_agent_tasks AS task
                WHERE task.task_id = '{before_task_id}'
                """,
            )
            == "true",
        )
        _docker("kill", "--signal", "KILL", before_worker_a)
        _docker("kill", "--signal", "KILL", lock_name)
        terminated = _psql_scalar(
            postgres_name,
            f"""
            WITH target AS MATERIALIZED (
                SELECT pid
                FROM pg_stat_activity
                WHERE application_name = '{lock_application_name}'
                  AND pid <> pg_backend_pid()
                ORDER BY backend_start
                LIMIT 1
            )
            SELECT count(*)
            FROM target
            WHERE pg_terminate_backend(target.pid)
            """,
        )
        if terminated != "1":
            raise RecoveryE2EError(
                "the isolated context-lock backend was not terminated"
            )
        _wait_until(
            "pre-send lease expiry",
            lambda: _psql_scalar(
                postgres_name,
                f"""
                SELECT (lease_expires_at <= clock_timestamp())::text
                FROM arena_agent_tasks
                WHERE task_id = '{before_task_id}'
                """,
            )
            == "true",
            timeout_seconds=40,
        )
        _run_worker(
            name=before_worker_b,
            network=network,
            key_volume=key_volume,
            image=image,
            worker_url=worker_url,
            worker_id=before_id_b,
        )
        add_container(before_worker_b)
        _wait_until(
            "pre-send task completion after restart",
            lambda: _psql_scalar(
                postgres_name,
                f"""
                SELECT status
                FROM arena_agent_tasks
                WHERE task_id = '{before_task_id}'
                """,
            )
            == "completed",
            timeout_seconds=30,
        )
        _docker("stop", "--time", "5", before_worker_b)
        before_snapshot = _controller(
            image=image,
            network=network,
            key_volume=key_volume,
            arguments=(
                "internal-snapshot",
                "--admin-url",
                admin_url,
                "--task-id",
                before_task_id,
            ),
        )
        before_provider = _http_json(
            provider_port,
            "/control/status",
        )
        before_attempts = before_snapshot.get("attempts")
        before_results = before_snapshot.get("results")
        if not (
            isinstance(before_attempts, list)
            and len(before_attempts) == 1
            and before_attempts[0]["attempt_no"] == 1
            and before_attempts[0]["worker_id"] == before_id_b
            and before_attempts[0]["status"] == "succeeded"
            and isinstance(before_results, list)
            and len(before_results) == 1
            and before_results[0]["runtime_status"] == "succeeded"
            and before_provider.get("modelRequests") == 2
        ):
            raise RecoveryE2EError(
                "pre-send recovery invariant failed: "
                f"{json.dumps(before_snapshot, sort_keys=True)} "
                f"{json.dumps(before_provider, sort_keys=True)}"
            )

        _http_json(provider_port, "/control/reset", method="POST", payload={})
        _http_json(
            provider_port,
            "/control/mode",
            method="POST",
            payload={"mode": "block"},
        )
        after = _controller(
            image=image,
            network=network,
            key_volume=key_volume,
            arguments=(
                "internal-seed-task",
                "--suffix",
                suffix,
                "--label",
                "after-send",
                "--round-index",
                "22",
                "--admin-url",
                admin_url,
                "--game-id",
                game_id,
                "--game-agent-id",
                game_agent_id,
            ),
        )
        after_task_id = str(after["taskId"])
        after_id_a = f"external-after-a-{suffix}"
        after_id_b = f"external-after-b-{suffix}"
        _run_worker(
            name=after_worker_a,
            network=network,
            key_volume=key_volume,
            image=image,
            worker_url=worker_url,
            worker_id=after_id_a,
        )
        add_container(after_worker_a)
        _wait_until(
            "durable request_sent before process kill",
            lambda: (
                _psql_scalar(
                    postgres_name,
                    f"""
                    SELECT count(*)
                    FROM arena_agent_task_attempts
                    WHERE task_id = '{after_task_id}'
                      AND status = 'request_sent'
                      AND request_sent_at IS NOT NULL
                    """,
                )
                == "1"
                and _http_json(
                    provider_port,
                    "/control/status",
                ).get("modelRequests")
                == 1
            ),
        )
        _docker("kill", "--signal", "KILL", after_worker_a)
        _http_json(
            provider_port,
            "/control/mode",
            method="POST",
            payload={"mode": "respond"},
        )
        _wait_until(
            "post-send lease expiry",
            lambda: _psql_scalar(
                postgres_name,
                f"""
                SELECT (lease_expires_at <= clock_timestamp())::text
                FROM arena_agent_tasks
                WHERE task_id = '{after_task_id}'
                """,
            )
            == "true",
            timeout_seconds=40,
        )
        _run_worker(
            name=after_worker_b,
            network=network,
            key_volume=key_volume,
            image=image,
            worker_url=worker_url,
            worker_id=after_id_b,
        )
        add_container(after_worker_b)
        _wait_until(
            "post-send task terminalization without replay",
            lambda: _psql_scalar(
                postgres_name,
                f"""
                SELECT status || '|' || terminal_reason
                FROM arena_agent_tasks
                WHERE task_id = '{after_task_id}'
                """,
            )
            == "defaulted|request_outcome_unknown",
            timeout_seconds=20,
        )
        _docker("stop", "--time", "5", after_worker_b)
        time.sleep(0.2)
        after_snapshot = _controller(
            image=image,
            network=network,
            key_volume=key_volume,
            arguments=(
                "internal-snapshot",
                "--admin-url",
                admin_url,
                "--task-id",
                after_task_id,
            ),
        )
        after_provider = _http_json(provider_port, "/control/status")
        after_attempts = after_snapshot.get("attempts")
        after_results = after_snapshot.get("results")
        if not (
            isinstance(after_attempts, list)
            and len(after_attempts) == 1
            and after_attempts[0]["attempt_no"] == 1
            and after_attempts[0]["worker_id"] == after_id_a
            and after_attempts[0]["status"] == "unknown"
            and after_attempts[0]["error_class"]
            == "request_outcome_unknown"
            and isinstance(after_results, list)
            and len(after_results) == 1
            and after_results[0]["runtime_status"] == "failed"
            and after_results[0]["error_class"]
            == "request_outcome_unknown"
            and after_provider.get("modelRequests") == 1
        ):
            raise RecoveryE2EError(
                "post-send recovery invariant failed: "
                f"{json.dumps(after_snapshot, sort_keys=True)} "
                f"{json.dumps(after_provider, sort_keys=True)}"
            )

        print(
            json.dumps(
                {
                    "schemaMigrations": _psql_scalar(
                        postgres_name,
                        "SELECT count(*) FROM adx_schema_migrations",
                    ),
                    "beforeRequestSent": {
                        "taskId": before_task_id,
                        "crashedWorkerId": before_id_a,
                        "recoveryWorkerId": before_id_b,
                        "observedBeforeKill": {
                            "taskStatus": "leased",
                            "leasedBy": before_id_a,
                            "attemptCount": 0,
                        },
                        "providerRequests": before_provider["modelRequests"],
                        "snapshot": before_snapshot,
                    },
                    "afterRequestSent": {
                        "taskId": after_task_id,
                        "crashedWorkerId": after_id_a,
                        "recoveryWorkerId": after_id_b,
                        "observedBeforeKill": {
                            "attemptStatus": "request_sent",
                            "attemptWorkerId": after_id_a,
                            "providerRequests": 1,
                        },
                        "providerRequests": after_provider["modelRequests"],
                        "snapshot": after_snapshot,
                    },
                    "schemaHead": _psql_scalar(
                        postgres_name,
                        """
                        SELECT migration_name
                        FROM adx_schema_migrations
                        ORDER BY migration_name DESC
                        LIMIT 1
                        """,
                    ),
                    "paymentsEnabled": False,
                    "status": "passed",
                },
                indent=2,
                sort_keys=True,
            ),
            flush=True,
        )
    except Exception:
        for name in (
            validation_worker,
            before_worker_a,
            before_worker_b,
            after_worker_a,
            after_worker_b,
        ):
            if name in created_containers:
                logs = _worker_logs(name)
                if logs:
                    print(f"\n--- {name} logs ---\n{logs}", file=sys.stderr)
        raise
    finally:
        if not args.keep:
            for name in reversed(created_containers):
                _docker("rm", "-f", name, check=False)
            _docker("network", "rm", network, check=False)
            _docker("volume", "rm", key_volume, check=False)
            if built_image:
                _docker("image", "rm", image, check=False)
        else:
            print(
                json.dumps(
                    {
                        "keptNetwork": network,
                        "keptKeyVolume": key_volume,
                        "keptContainers": created_containers,
                        "image": image,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the external Hosted Worker process-recovery E2E."
    )
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run")
    run.add_argument(
        "--image",
        help="Use a prebuilt API image instead of building the current source.",
    )
    run.add_argument(
        "--keep",
        action="store_true",
        help="Keep disposable containers, network, volume, and built image.",
    )

    fake = subparsers.add_parser("fake-provider")
    fake.add_argument("--port", type=int, default=4000)

    seed_agent = subparsers.add_parser("internal-seed-agent")
    seed_agent.add_argument("--suffix", required=True)
    seed_agent.add_argument("--admin-url", required=True)
    seed_agent.add_argument("--api-url", required=True)

    wait_agent = subparsers.add_parser("internal-wait-agent")
    wait_agent.add_argument("--api-url", required=True)
    wait_agent.add_argument("--owner-id", required=True)
    wait_agent.add_argument("--agent-id", required=True)
    wait_agent.add_argument("--timeout", type=float, default=30)

    seed_game = subparsers.add_parser("internal-seed-game")
    seed_game.add_argument("--suffix", required=True)
    seed_game.add_argument("--admin-url", required=True)
    seed_game.add_argument("--api-url", required=True)
    seed_game.add_argument("--owner-id", required=True)
    seed_game.add_argument("--agent-id", required=True)

    seed_task = subparsers.add_parser("internal-seed-task")
    seed_task.add_argument("--suffix", required=True)
    seed_task.add_argument("--label", required=True)
    seed_task.add_argument("--round-index", type=int, required=True)
    seed_task.add_argument("--admin-url", required=True)
    seed_task.add_argument("--game-id", required=True)
    seed_task.add_argument("--game-agent-id", required=True)

    snapshot = subparsers.add_parser("internal-snapshot")
    snapshot.add_argument("--admin-url", required=True)
    snapshot.add_argument("--task-id", required=True)
    return parser


def main() -> None:
    parser = _parser()
    arguments = parser.parse_args(sys.argv[1:] or ["run"])
    command = arguments.command
    if command == "run":
        _orchestrate(arguments)
    elif command == "fake-provider":
        _run_fake_provider(arguments.port)
    elif command == "internal-seed-agent":
        asyncio.run(_internal_seed_agent(arguments))
    elif command == "internal-wait-agent":
        asyncio.run(_internal_wait_agent(arguments))
    elif command == "internal-seed-game":
        asyncio.run(_internal_seed_game(arguments))
    elif command == "internal-seed-task":
        asyncio.run(_internal_seed_task(arguments))
    elif command == "internal-snapshot":
        asyncio.run(_internal_snapshot(arguments))
    else:  # pragma: no cover - argparse owns this branch
        parser.error("unknown command")


if __name__ == "__main__":
    main()
