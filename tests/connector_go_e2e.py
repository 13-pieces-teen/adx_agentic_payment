"""No-model cross-language smoke for the real Go Connector.

Run with the local FastAPI server helper:

    $env:ADX_CONNECTOR_UNSAFE_DEMO='true'
    python <with_server.py> \
      --server "python -m uvicorn web.api:create_app --factory --host 127.0.0.1 --port 8000" \
      --port 8000 \
      -- python tests/connector_go_e2e.py

The smoke builds and starts the Connector, injects a one-time enrolled device
credential, waits for real local runtime discovery, and verifies a typed
``runtime.probe`` command reaches ``succeeded``. It never launches a paid model
task.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


API_BASE = "http://127.0.0.1:8000"
WORKSPACE = Path(__file__).resolve().parents[1]
CONNECTOR_ROOT = WORKSPACE / "connector"


def api(method: str, path: str, body: dict | None = None) -> dict:
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        API_BASE + path,
        data=encoded,
        method=method,
        headers={"Content-Type": "application/json"} if encoded else {},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_until(description: str, callback, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    last_value = None
    while time.monotonic() < deadline:
        last_value = callback()
        if last_value:
            return last_value
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {description}; last={last_value!r}")


def go_executable() -> str:
    configured = os.environ.get("GO_EXE")
    candidates = [
        configured,
        shutil.which("go"),
        r"E:\Go\bin\go.exe" if os.name == "nt" else None,
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise RuntimeError("Go was not found; set GO_EXE to the configured executable")


def enroll() -> dict:
    pairing = api(
        "POST",
        "/api/connectors/pairings",
        {"device_name": "Go E2E computer"},
    )
    api(
        "POST",
        f"/api/connectors/pairings/{pairing['user_code']}/approve",
        {"owner_id": "go-e2e-owner"},
    )
    return api(
        "POST",
        "/api/connectors/pairings/exchange",
        {"device_code": pairing["device_code"]},
    )


def main() -> None:
    credential = enroll()
    device_id = credential["device_id"]

    with tempfile.TemporaryDirectory(prefix="adx-connector-go-e2e-") as temp:
        temp_path = Path(temp)
        binary = temp_path / (
            "adx-connector.exe" if os.name == "nt" else "adx-connector"
        )
        subprocess.run(
            [
                go_executable(),
                "build",
                "-trimpath",
                "-o",
                str(binary),
                "./cmd/adx-connector",
            ],
            cwd=CONNECTOR_ROOT,
            check=True,
            timeout=120,
        )

        environment = os.environ.copy()
        environment.update(
            {
                "ADX_CONNECTOR_DEVICE_ID": device_id,
                "ADX_CONNECTOR_TOKEN": credential["device_token"],
                "ADX_CONNECTOR_GATEWAY_URL": (
                    f"ws://127.0.0.1:8000{credential['ws_url']}"
                ),
            }
        )
        connector = subprocess.Popen(
            [
                str(binary),
                "run",
                "--auto-pair=false",
                "--state",
                str(temp_path / "state.json"),
                "--allow-root",
                str(WORKSPACE),
                "--heartbeat",
                "1s",
                "--inventory-interval",
                "5m",
            ],
            cwd=WORKSPACE,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:

            def online_device():
                device = api("GET", f"/api/connectors/devices/{device_id}")
                return (
                    device
                    if device["status"] == "online" and device["runtimes"]
                    else None
                )

            device = wait_until("Connector inventory", online_device)
            runtime = next(
                (
                    item
                    for item in device["runtimes"]
                    if item["kind"] in {"codex", "claude-code"}
                    and item.get("available", True)
                ),
                None,
            )
            if runtime is None:
                raise AssertionError(
                    f"no usable Claude Code or Codex runtime: {device['runtimes']!r}"
                )

            binding = api(
                "POST",
                f"/api/connectors/devices/{device_id}/bindings",
                {
                    "runtime_id": runtime["runtime_id"],
                    "display_name": "Go E2E binding",
                },
            )
            command = api(
                "POST",
                f"/api/connectors/bindings/{binding['binding_id']}/commands",
                {
                    "action": "runtime.probe",
                    "payload": {},
                    "idempotency_key": "go-e2e-runtime-probe",
                },
            )

            def succeeded_command():
                commands = api(
                    "GET",
                    f"/api/connectors/bindings/{binding['binding_id']}/commands",
                )["commands"]
                current = next(
                    (
                        item
                        for item in commands
                        if item["command_id"] == command["command_id"]
                    ),
                    None,
                )
                if current and current["status"] in {"failed", "rejected", "expired"}:
                    raise AssertionError(f"runtime.probe failed: {current!r}")
                return current if current and current["status"] == "succeeded" else None

            wait_until("runtime.probe succeeded", succeeded_command)
            print(
                json.dumps(
                    {
                        "device_online": True,
                        "runtime_kind": runtime["kind"],
                        "runtime_version": runtime.get("version"),
                        "binding_created": True,
                        "runtime_probe": "succeeded",
                    }
                )
            )
        finally:
            connector.terminate()
            try:
                stdout, stderr = connector.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                connector.kill()
                stdout, stderr = connector.communicate(timeout=5)
            if connector.returncode not in {0, 1, -15}:
                raise AssertionError(
                    "Connector exited unexpectedly: "
                    f"code={connector.returncode}, stdout={stdout[-2000:]!r}, "
                    f"stderr={stderr[-2000:]!r}"
                )


if __name__ == "__main__":
    main()
