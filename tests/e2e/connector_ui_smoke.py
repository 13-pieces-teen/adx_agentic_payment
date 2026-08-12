"""Manual Playwright smoke for the local Connector management page.

Run through the webapp-testing server helper:

    $env:ADX_CONNECTOR_UNSAFE_DEMO='true'
    python <with_server.py> \
      --server "python -m uvicorn web.api:create_app --factory --port 8000" --port 8000 \
      --server "cd frontend && npm run dev -- --port 3000" --port 3000 \
      -- python tests/e2e/connector_ui_smoke.py
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect, sync_playwright
from websockets.sync.client import connect


API_BASE = "http://127.0.0.1:8000"
WEB_BASE = "http://127.0.0.1:3000"
WORKSPACE = str(Path(__file__).resolve().parents[2])


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


def enroll_fake_connector() -> tuple[str, str]:
    pairing = api(
        "POST",
        "/api/connectors/pairings",
        {"device_name": "Playwright computer"},
    )
    api(
        "POST",
        f"/api/connectors/pairings/{pairing['user_code']}/approve",
        {"owner_id": "demo-user"},
    )
    credential = api(
        "POST",
        "/api/connectors/pairings/exchange",
        {"device_code": pairing["device_code"]},
    )
    return credential["device_id"], credential["device_token"]


def fake_connector(device_id: str, token: str, stop: threading.Event) -> None:
    url = f"ws://127.0.0.1:8000/api/connectors/ws?device_id={device_id}"
    with connect(
        url,
        additional_headers={"Authorization": f"Device {token}"},
        open_timeout=5,
    ) as socket:
        json.loads(socket.recv(timeout=5))
        socket.send(
            json.dumps(
                {
                    "type": "hello",
                    "protocol_version": "1.0",
                    "device_id": device_id,
                    "message_id": "ui-hello",
                    "sequence": 1,
                    "payload": {
                        "protocol_version": "1.0",
                        "connector_version": "ui-smoke",
                        "platform": "windows/amd64",
                        "hostname": "playwright-pc",
                    },
                }
            )
        )
        json.loads(socket.recv(timeout=5))
        socket.send(
            json.dumps(
                {
                    "type": "inventory.snapshot",
                    "protocol_version": "1.0",
                    "device_id": device_id,
                    "message_id": "ui-inventory",
                    "sequence": 2,
                    "payload": {
                        "runtimes": [
                            {
                                "runtime_id": "codex-ui-smoke",
                                "kind": "codex",
                                "display_name": "Codex UI Smoke",
                                "executable_path": "C:\\tools\\codex.exe",
                                "version": "test",
                                "available": True,
                                "capabilities": [
                                    "session.start",
                                    "task.dispatch",
                                    "task.cancel",
                                    "session.stop",
                                    "session.resume",
                                ],
                                "auth_modes": ["test"],
                            }
                        ]
                    },
                }
            )
        )
        json.loads(socket.recv(timeout=5))
        while not stop.wait(0.25):
            socket.send(
                json.dumps(
                    {
                        "type": "heartbeat",
                        "protocol_version": "1.0",
                        "device_id": device_id,
                        "message_id": f"heartbeat-{time.time_ns()}",
                        "payload": {"active_sessions": 0},
                    }
                )
            )
            json.loads(socket.recv(timeout=5))


def main() -> None:
    device_id, token = enroll_fake_connector()
    stop = threading.Event()
    connector_thread = threading.Thread(
        target=fake_connector,
        args=(device_id, token, stop),
        daemon=True,
    )
    connector_thread.start()

    console_errors: list[str] = []
    response_errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            page.on(
                "console",
                lambda message: (
                    console_errors.append(
                        f"{message.text} @ {message.location.get('url', '')}"
                    )
                    if message.type == "error"
                    else None
                ),
            )
            page.on(
                "response",
                lambda response: (
                    response_errors.append(f"{response.status} {response.url}")
                    if response.status >= 400
                    else None
                ),
            )
            page.goto(
                f"{WEB_BASE}/agents#connect",
                wait_until="domcontentloaded",
                timeout=90_000,
            )
            # The Connector console intentionally polls device state, and the
            # Next dev server keeps its HMR channel open. Give network idle a
            # chance, then use the product heading as the semantic ready signal.
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeoutError:
                pass

            heading = page.get_by_role(
                "heading", name="Bring a runtime into the Arena."
            )
            if heading.count() == 0:
                diagnostic_path = os.path.join(
                    os.environ.get("TEMP", "."),
                    "adx-connector-ui-diagnostic.png",
                )
                page.screenshot(path=diagnostic_path, full_page=True)
                body = page.locator("body").inner_text()[:2000]
                raise AssertionError(
                    f"connector heading missing at {page.url}; body={body!r}"
                )
            expect(heading).to_be_visible()
            expect(page.get_by_text("Codex UI Smoke")).to_be_visible(timeout=10_000)
            identity = page.get_by_label("Arena identity")
            expect(identity).to_be_visible()
            identity.select_option("agent-ui-existing")
            expect(identity).to_have_value("agent-ui-existing")
            page.get_by_role("button", name="Bind runtime").click()
            expect(
                page.get_by_text("Existing Arena Agent is now bound to Codex UI Smoke.")
            ).to_be_visible(timeout=10_000)
            bindings = api("GET", "/api/connectors/bindings")["bindings"]
            if not bindings or bindings[0]["agent_id"] != "agent-ui-existing":
                raise AssertionError(
                    "runtime was not bound to the selected Arena Agent: "
                    f"{bindings!r}"
                )
            workspace = page.get_by_label("Managed session workspace")
            expect(workspace).to_be_visible()
            start = page.get_by_role("button", name="Start managed session")
            expect(start).to_be_disabled()
            expect(
                page.get_by_text(
                    "ADX cannot supply or replace that token.",
                    exact=False,
                )
            ).to_be_visible()
            if page.get_by_label("Conversation ID to resume").count() != 0:
                raise AssertionError(
                    "cloud-supplied conversation id input is still exposed"
                )
            expect(
                page.get_by_role("button", name="Resume managed session")
            ).to_be_disabled()
            workspace.fill(WORKSPACE)
            expect(start).to_be_enabled()

            screenshot_path = os.path.join(
                os.environ.get("TEMP", "."), "adx-connector-ui-smoke.png"
            )
            page.screenshot(path=screenshot_path, full_page=True)
            browser.close()
    finally:
        stop.set()
        connector_thread.join(timeout=3)

    if console_errors or response_errors:
        raise AssertionError(
            f"browser console errors: {console_errors}; "
            f"HTTP errors: {response_errors}"
        )
    print(
        json.dumps(
            {
                "page": "agents#connect",
                "runtime_visible": True,
                "identity_boundary": True,
                "binding_created": True,
                "workspace_guard": True,
                "resume_token_boundary": True,
            }
        )
    )


if __name__ == "__main__":
    main()
