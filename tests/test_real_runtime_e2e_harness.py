from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from real_runtimes_docker_e2e import RealConnector, UserSession


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
