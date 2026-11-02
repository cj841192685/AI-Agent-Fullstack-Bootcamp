from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.config import RateLimitConfig
from app.core.errors import GatewayError


@dataclass
class Bucket:
    tokens: float
    updated_at: float


class InMemoryRateLimiter:
    """Process-local token bucket. Use Redis when running multiple replicas."""

    def __init__(self, config: RateLimitConfig) -> None:
        self.config = config
        self._buckets: dict[str, Bucket] = {}
        self._lock = asyncio.Lock()

    async def check(self, identity: str) -> None:
        if not self.config.enabled:
            return
        now = time.monotonic()
        refill_per_second = self.config.requests_per_minute / 60
        capacity = float(self.config.burst)
        async with self._lock:
            bucket = self._buckets.setdefault(identity, Bucket(capacity, now))
            bucket.tokens = min(capacity, bucket.tokens + (now - bucket.updated_at) * refill_per_second)
            bucket.updated_at = now
            if bucket.tokens < 1:
                retry_after = max(1, int((1 - bucket.tokens) / refill_per_second))
                raise GatewayError(
                    f"Rate limit exceeded. Retry in {retry_after}s",
                    status_code=429,
                    error_type="rate_limit_error",
                    code="rate_limit_exceeded",
                )
            bucket.tokens -= 1


class InMemoryConcurrencyLimiter:
    """Process-local concurrency bound; use a shared limiter for multiple replicas."""

    def __init__(self, max_concurrent_requests: int) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)

    async def acquire(self) -> None:
        await self._semaphore.acquire()

    def release(self) -> None:
        self._semaphore.release()
