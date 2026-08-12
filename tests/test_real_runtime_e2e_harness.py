from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, web
import httpx

from tests.e2e.mixed_codex_fallback_docker_e2e import MixedRuntimeFaultProxy
from tests.e2e.real_runtimes_docker_e2e import RealConnector, UserSession


class _LiveProcess:
    def poll(self) -> None:
        return None


def test_wait_online_reuses_runtime_without_creating_second_binding(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/connectors/devices/device-1"
        return httpx.Response(
            200,
            request=request,
            json={
                "runtimes": [
                    {
                        "runtime_id": "runtime-1",
                        "kind": "codex",
                        "version": "test",
                        "available": True,
                        "task_enabled": True,
                        "authentication_status": "configured",
                        "arena_compatible": True,
                        "local_execution_ready": True,
                        "capabilities": ["session.start", "task.dispatch"],
                    }
                ]
            },
        )

    async def exercise() -> tuple[RealConnector, dict[str, object]]:
        async with httpx.AsyncClient(
            base_url="http://arena.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            connector = RealConnector(
                kind="codex",
                label="restart",
                user=UserSession(client=client, csrf_token="csrf"),
                credential={"device_id": "device-1", "device_token": "token"},
                connector_executable=tmp_path / "connector",
                temp_root=tmp_path,
                codex_shim_root=tmp_path,
                run_id="run",
            )
            connector.process = _LiveProcess()  # type: ignore[assignment]
            runtime = await connector.wait_online()
            return connector, runtime

    connector, runtime = asyncio.run(exercise())

    assert runtime["runtime_id"] == "runtime-1"
    assert connector.runtime == runtime
    assert len(requests) == 1


def test_fault_proxy_rejects_only_the_armed_terminal_result_once() -> None:
    async def exercise() -> tuple[int, int]:
        upstream = web.Application()

        async def accept(_: web.Request) -> web.Response:
            return web.json_response({"forwarded": True})

        upstream.router.add_post("/mcp", accept)
        upstream_runner = web.AppRunner(upstream)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]
        proxy = MixedRuntimeFaultProxy(
            upstream_origin=f"http://127.0.0.1:{upstream_port}",
        )
        await proxy.start()
        proxy.fail_next_terminal_result("task-1")
        payload = {
            "jsonrpc": "2.0",
            "id": "request-1",
            "method": "tools/call",
            "params": {
                "name": "arena_submit_agent_task_result",
                "arguments": {
                    "result": {
                        "taskId": "task-1",
                        "resultId": "result-1",
                    }
                },
            },
        }
        try:
            async with httpx.AsyncClient() as client:
                failed = await client.post(f"{proxy.origin}/mcp", json=payload)
                replayed = await client.post(f"{proxy.origin}/mcp", json=payload)
            await proxy.wait_for_terminal_result_failure(timeout_seconds=1)
            return failed.status_code, replayed.status_code
        finally:
            await proxy.stop()
            await upstream_runner.cleanup()

    assert asyncio.run(exercise()) == (503, 200)


def test_fault_proxy_transparently_bridges_websocket_messages() -> None:
    async def exercise() -> str:
        upstream = web.Application()

        async def echo(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            async for message in socket:
                if message.type == WSMsgType.TEXT:
                    await socket.send_str(f"upstream:{message.data}")
            return socket

        upstream.router.add_get("/socket", echo)
        upstream_runner = web.AppRunner(upstream)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]
        proxy = MixedRuntimeFaultProxy(
            upstream_origin=f"http://127.0.0.1:{upstream_port}",
        )
        await proxy.start()
        try:
            async with ClientSession() as client:
                async with client.ws_connect(
                    f"{proxy.origin}/socket"
                ) as socket:
                    await socket.send_str("hello")
                    message = await socket.receive(timeout=1)
                    assert message.type == WSMsgType.TEXT
                    return str(message.data)
        finally:
            await proxy.stop()
            await upstream_runner.cleanup()

    assert asyncio.run(exercise()) == "upstream:hello"


def test_fault_proxy_injects_an_orphan_lease_before_forwarding_claim() -> None:
    async def exercise() -> tuple[int, list[str]]:
        intercepted: list[str] = []

        async def inject(task_id: str) -> bool:
            intercepted.append(task_id)
            return True

        upstream = web.Application()

        async def accept(_: web.Request) -> web.Response:
            return web.json_response({"forwarded": True})

        upstream.router.add_post("/mcp", accept)
        upstream_runner = web.AppRunner(upstream)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]
        proxy = MixedRuntimeFaultProxy(
            upstream_origin=f"http://127.0.0.1:{upstream_port}",
            orphan_claim_injector=inject,
        )
        await proxy.start()
        payload = {
            "jsonrpc": "2.0",
            "id": "request-1",
            "method": "tools/call",
            "params": {
                "name": "arena_claim_agent_task",
                "arguments": {"taskId": "task-1"},
            },
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{proxy.origin}/mcp",
                    json=payload,
                )
            task_id = await proxy.wait_for_orphan_lease_injection(
                timeout_seconds=1,
            )
            assert task_id == "task-1"
            return response.status_code, intercepted
        finally:
            await proxy.stop()
            await upstream_runner.cleanup()

    assert asyncio.run(exercise()) == (200, ["task-1"])
