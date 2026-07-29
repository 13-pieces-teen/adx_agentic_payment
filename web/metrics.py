"""Small dependency-free Prometheus metrics for the Arena API process."""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send


_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


class ApiMetrics:
    def __init__(self) -> None:
        self.inflight = 0
        self.requests: Counter[tuple[str, int]] = Counter()
        self.duration_count = 0
        self.duration_sum = 0.0
        self.duration_buckets: Counter[float] = Counter()
        self.readiness_failures = 0

    def observe(self, method: str, status: int, duration: float) -> None:
        self.requests[(method, status)] += 1
        self.duration_count += 1
        self.duration_sum += duration
        for bucket in _BUCKETS:
            if duration <= bucket:
                self.duration_buckets[bucket] += 1

    def render(self, repositories: dict[str, object]) -> str:
        lines = [
            "# HELP arena_http_requests_total HTTP requests by method and status.",
            "# TYPE arena_http_requests_total counter",
        ]
        for (method, status), count in sorted(self.requests.items()):
            lines.append(
                'arena_http_requests_total'
                f'{{method="{method}",status="{status}"}} {count}'
            )
        lines.extend(
            [
                "# HELP arena_http_requests_inflight Current HTTP requests.",
                "# TYPE arena_http_requests_inflight gauge",
                f"arena_http_requests_inflight {self.inflight}",
                "# HELP arena_http_request_duration_seconds HTTP request latency.",
                "# TYPE arena_http_request_duration_seconds histogram",
            ]
        )
        for bucket in _BUCKETS:
            lines.append(
                "arena_http_request_duration_seconds_bucket"
                f'{{le="{bucket:g}"}} {self.duration_buckets[bucket]}'
            )
        lines.extend(
            [
                "arena_http_request_duration_seconds_bucket"
                f'{{le="+Inf"}} {self.duration_count}',
                f"arena_http_request_duration_seconds_sum {self.duration_sum:.9f}",
                f"arena_http_request_duration_seconds_count {self.duration_count}",
                "# HELP arena_readiness_failures_total Failed readiness probes.",
                "# TYPE arena_readiness_failures_total counter",
                f"arena_readiness_failures_total {self.readiness_failures}",
                "# HELP arena_postgres_pool_connections PostgreSQL pool connections.",
                "# TYPE arena_postgres_pool_connections gauge",
            ]
        )
        seen: set[int] = set()
        for name, repository in repositories.items():
            pool = getattr(repository, "_pool", None)
            if pool is None or id(pool) in seen:
                continue
            seen.add(id(pool))
            get_size = getattr(pool, "get_size", None)
            get_idle_size = getattr(pool, "get_idle_size", None)
            if callable(get_size):
                lines.append(
                    "arena_postgres_pool_connections"
                    f'{{repository="{name}",state="open"}} {int(get_size())}'
                )
            if callable(get_idle_size):
                lines.append(
                    "arena_postgres_pool_connections"
                    f'{{repository="{name}",state="idle"}} '
                    f"{int(get_idle_size())}"
                )
        return "\n".join(lines) + "\n"


class ApiMetricsMiddleware:
    def __init__(self, app: ASGIApp, *, metrics: ApiMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "UNKNOWN"))
        started = time.perf_counter()
        status = 500
        self.metrics.inflight += 1

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            self.metrics.inflight -= 1
            self.metrics.observe(
                method,
                status,
                time.perf_counter() - started,
            )


async def postgres_readiness(
    repositories: dict[str, object],
) -> dict[str, str]:
    """Probe each distinct configured pool without exposing SQL errors."""

    async def probe(name: str, pool: Any) -> tuple[str, str]:
        try:
            value = await pool.fetchval("SELECT 1")
            return name, "ok" if value == 1 else "unavailable"
        except Exception:
            return name, "unavailable"

    unique: dict[int, tuple[str, Any]] = {}
    results: dict[str, str] = {}
    for name, repository in repositories.items():
        pool = getattr(repository, "_pool", None)
        if pool is None:
            results[name] = "unavailable"
            continue
        unique.setdefault(id(pool), (name, pool))
    if unique:
        import asyncio

        checked = await asyncio.wait_for(
            asyncio.gather(
                *(probe(name, pool) for name, pool in unique.values())
            ),
            timeout=3.0,
        )
        results.update(checked)
    return results


__all__ = [
    "ApiMetrics",
    "ApiMetricsMiddleware",
    "postgres_readiness",
]
