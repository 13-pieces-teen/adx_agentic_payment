"""Bounded rate limiting for the single-writer public control plane."""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitExceeded(Exception):
    retry_after_seconds: int


class SlidingWindowRateLimiter:
    """In-memory limiter matching the production control-plane boundary."""

    def __init__(
        self,
        attempts: int,
        window_seconds: int,
        max_keys: int = 4096,
    ) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._buckets: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        async with self._lock:
            self._purge_expired_buckets(cutoff)
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.max_keys:
                    # Refuse to allocate attacker-controlled keys indefinitely.
                    raise RateLimitExceeded(self.window_seconds)
                bucket = deque()
                self._buckets[key] = bucket
            else:
                self._buckets.move_to_end(key)

            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.attempts:
                retry_after = max(
                    1,
                    math.ceil(bucket[0] + self.window_seconds - now),
                )
                raise RateLimitExceeded(retry_after)
            bucket.append(now)

    def _purge_expired_buckets(self, cutoff: float) -> None:
        while self._buckets:
            key, bucket = next(iter(self._buckets.items()))
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if bucket:
                break
            self._buckets.pop(key, None)
