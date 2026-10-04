"""Offline demo provider: deterministic fake results, no network traffic.

Useful for trying the dashboard, resume and caching behaviour without sending a
single request to Mojang. Its results are clearly labelled ``demo`` and the CLI
stores them in a separate database and output directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING

from ..http_client import RequestStats
from ..models import CheckResult, Confidence, Status
from ..rate_limit import AsyncRateLimiter, Clock, Sleep
from ..timeutil import utcnow
from .base import Provider, ProviderInfo, ProviderRole

if TYPE_CHECKING:
    from ..config import AppConfig, DemoProviderConfig, ScannerConfig


def _fraction(name: str, salt: str = "") -> float:
    digest = hashlib.blake2b(f"{salt}{name.lower()}".encode(), digest_size=8).digest()
    return int.from_bytes(digest) / 2**64


class DemoProvider(Provider):
    name = "demo"
    display_name = "Demo (offline)"
    role = ProviderRole.OFFLINE
    max_batch_size = 10

    def __init__(
        self,
        config: DemoProviderConfig,
        scanner: ScannerConfig,
        *,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._limiter = AsyncRateLimiter(
            scanner.requests_per_second, burst=scanner.burst, clock=clock, sleep=sleep, name="demo"
        )
        self._stats = RequestStats()

    @classmethod
    def info(cls, config: AppConfig) -> ProviderInfo:
        return ProviderInfo(
            name=cls.name,
            display_name=cls.display_name,
            role=cls.role,
            description="Simulated results for trying the UI, caching and resume. No network access.",
            endpoints=(),
            rate_limit="simulated (uses --rps)",
        )

    @property
    def limiter(self) -> AsyncRateLimiter:
        return self._limiter

    @property
    def request_stats(self) -> RequestStats:
        return self._stats

    async def check(self, username: str) -> CheckResult:
        return (await self.check_many([username]))[0]

    async def check_many(self, usernames: Sequence[str]) -> list[CheckResult]:
        await self._limiter.acquire()
        self._stats.requests += 1
        self._stats.status_counts[200] = self._stats.status_counts.get(200, 0) + 1
        if self.config.latency:
            await self._sleep(self.config.latency * (0.5 + self._rng.random()))
        return [self._fake(name) for name in usernames]

    def _fake(self, name: str) -> CheckResult:
        if self._rng.random() < self.config.error_rate:
            return self._error(name, "simulated transient error")
        roll = _fraction(name)
        if roll < 0.06:
            return self._result(
                name, Status.AVAILABLE, confidence=Confidence.UNVERIFIED, detail="Simulated result."
            )
        if roll < 0.075:
            return self._result(
                name, Status.AVAILABLE, confidence=Confidence.CONFIRMED, detail="Simulated result."
            )
        if roll < 0.085:
            hours = 1 + int(_fraction(name, "release") * 36 * 24)
            return self._result(
                name,
                Status.SOON,
                confidence=Confidence.ESTIMATED,
                available_at=utcnow() + timedelta(hours=hours),
                detail="Simulated estimate.",
            )
        if roll < 0.09:
            return self._result(name, Status.SOON, detail="Simulated hold; release unknown.")
        if roll < 0.093:
            return self._result(name, Status.BLOCKED, detail="Simulated result.")
        uuid = hashlib.blake2b(name.lower().encode(), digest_size=16).hexdigest()
        return self._result(name, Status.TAKEN, confidence=Confidence.CONFIRMED, uuid=uuid, display_name=name)
