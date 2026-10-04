"""Adaptive asynchronous rate limiter.

A token bucket shared by every worker of a provider:

* ``rate`` tokens per second, at most ``burst`` saved up;
* :meth:`penalize` (called on HTTP 429) halves the rate and blocks *all* callers
  for a cooldown, honouring ``Retry-After`` when the server sends one;
* :meth:`slow_down` is a softer reduction for warning signals (e.g. 503s);
* :meth:`record_success` slowly restores the configured rate after a streak of
  successful requests (additive increase, multiplicative decrease).

Waiters are served in FIFO order because they queue on an ``asyncio.Lock``.
``clock`` and ``sleep`` are injectable so tests can use a fake clock.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

_EPSILON = 1e-9


class AsyncRateLimiter:
    def __init__(
        self,
        rate: float,
        burst: int = 1,
        *,
        min_rate: float | None = None,
        recovery_after: int = 50,
        recovery_step: float = 0.1,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        name: str = "limiter",
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.name = name
        self._base_rate = float(rate)
        self._rate = float(rate)
        self._min_rate = min_rate if min_rate is not None else max(rate / 16.0, 0.005)
        self._burst = max(1, int(burst))
        self._recovery_after = recovery_after
        self._recovery_step = recovery_step
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(self._burst)
        self._last = clock()
        self._blocked_until = 0.0
        self._successes = 0
        self._lock = asyncio.Lock()
        self.penalties = 0
        self.total_wait = 0.0

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def base_rate(self) -> float:
        return self._base_rate

    @property
    def is_throttled(self) -> bool:
        return self._rate < self._base_rate * 0.999

    @property
    def cooldown_remaining(self) -> float:
        return max(0.0, self._blocked_until - self._clock())

    def _refill(self, now: float) -> None:
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(float(self._burst), self._tokens + elapsed * self._rate)
            self._last = now

    async def acquire(self) -> float:
        """Wait for permission to send one request. Returns the time spent waiting."""
        started = self._clock()
        async with self._lock:
            while True:
                now = self._clock()
                if now < self._blocked_until:
                    await self._sleep(self._blocked_until - now)
                    continue
                self._refill(now)
                # The epsilon absorbs float rounding: a deficit of ~1e-15 tokens would otherwise
                # ask for sleeps too short to move a large monotonic clock value at all.
                if self._tokens >= 1.0 - _EPSILON:
                    self._tokens = max(0.0, self._tokens - 1.0)
                    waited = self._clock() - started
                    self.total_wait += waited
                    return waited
                await self._sleep((1.0 - self._tokens) / self._rate)

    async def __aenter__(self) -> AsyncRateLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def penalize(self, cooldown: float | None = None, *, factor: float = 0.5) -> None:
        """React to an HTTP 429: reduce the rate and pause every caller for ``cooldown`` seconds."""
        now = self._clock()
        self._refill(now)
        old_rate = self._rate
        self._rate = max(self._min_rate, self._rate * factor)
        self._tokens = 0.0
        self._successes = 0
        self.penalties += 1
        if cooldown is not None and cooldown > 0:
            self._blocked_until = max(self._blocked_until, now + cooldown)
            # No tokens accumulate while blocked: resume gently at the reduced rate.
            self._last = max(self._last, self._blocked_until)
        logger.info(
            "%s: rate %.3f -> %.3f req/s, cooldown %.1fs",
            self.name,
            old_rate,
            self._rate,
            cooldown or 0.0,
        )

    def slow_down(self, factor: float = 0.8) -> None:
        """Softer reduction without a pause (server warnings, 503s)."""
        self._refill(self._clock())
        self._rate = max(self._min_rate, self._rate * factor)
        self._successes = 0

    def record_success(self) -> None:
        self._successes += 1
        if self.is_throttled and self._successes >= self._recovery_after:
            self._successes = 0
            self._refill(self._clock())
            self._rate = min(self._base_rate, self._rate + self._base_rate * self._recovery_step)
            logger.debug("%s: recovering, rate now %.3f req/s", self.name, self._rate)
