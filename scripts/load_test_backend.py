"""Read-only HTTP load probe for Arena 402 readiness and capacity checks."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time

import httpx


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


async def _run(args: argparse.Namespace) -> dict[str, object]:
    semaphore = asyncio.Semaphore(args.concurrency)
    latencies: list[float] = []
    failures = 0
    timeout = httpx.Timeout(args.timeout_seconds)
    limits = httpx.Limits(
        max_connections=args.concurrency,
        max_keepalive_connections=args.concurrency,
    )
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        timeout=timeout,
        limits=limits,
        follow_redirects=False,
    ) as client:

        async def request_once() -> None:
            nonlocal failures
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.get(args.path)
                    if response.status_code not in args.accept_status:
                        failures += 1
                except httpx.HTTPError:
                    failures += 1
                finally:
                    latencies.append(time.perf_counter() - started)

        started = time.perf_counter()
        await asyncio.gather(
            *(request_once() for _ in range(args.requests))
        )
        elapsed = time.perf_counter() - started

    p95_ms = _percentile(latencies, 0.95) * 1000
    return {
        "baseUrl": args.base_url,
        "path": args.path,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "failures": failures,
        "errorRate": failures / args.requests,
        "elapsedSeconds": round(elapsed, 3),
        "requestsPerSecond": round(args.requests / elapsed, 2),
        "p50Ms": round(_percentile(latencies, 0.50) * 1000, 2),
        "p95Ms": round(p95_ms, 2),
        "p99Ms": round(_percentile(latencies, 0.99) * 1000, 2),
        "passed": (
            failures / args.requests <= args.max_error_rate
            and p95_ms <= args.max_p95_ms
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a bounded read-only Arena HTTP load probe."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--path", default="/api/ready")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--max-p95-ms", type=float, default=500.0)
    parser.add_argument(
        "--accept-status",
        type=int,
        nargs="+",
        default=[200],
    )
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1:
        parser.error("requests and concurrency must be positive")
    result = asyncio.run(_run(args))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
