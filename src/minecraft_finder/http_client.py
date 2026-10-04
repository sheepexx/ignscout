"""Rate-limited, retrying HTTP request executor shared by the network providers."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Collection
from dataclasses import dataclass, field, fields
from typing import Any
from urllib.parse import urlsplit

import httpx

from .rate_limit import AsyncRateLimiter, Sleep
from .retry import RETRYABLE_STATUS_CODES, RetryPolicy, parse_retry_after

logger = logging.getLogger(__name__)

#: Mojang responses report the caller's rate-limit state here ("UNDER_LIMIT" when fine).
RATE_LIMIT_HEADER = "x-minecraft-rate-limit-result"


class RequestFailed(Exception):
    """A request could not be completed, even after retries."""

    def __init__(self, message: str, *, status_code: int | None = None, attempts: int = 1) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts


class RateLimited(RequestFailed):
    def __init__(self, message: str, *, retry_after: float | None = None, attempts: int = 1) -> None:
        super().__init__(message, status_code=429, attempts=attempts)
        self.retry_after = retry_after


@dataclass
class RequestStats:
    requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    timeouts: int = 0
    network_errors: int = 0
    server_errors: int = 0
    status_counts: dict[int, int] = field(default_factory=dict)

    @classmethod
    def merged(cls, *parts: RequestStats) -> RequestStats:
        total = cls()
        for part in parts:
            for f in fields(cls):
                if f.name == "status_counts":
                    for code, count in part.status_counts.items():
                        total.status_counts[code] = total.status_counts.get(code, 0) + count
                else:
                    setattr(total, f.name, getattr(total, f.name) + getattr(part, f.name))
        return total


def _where(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}"


class HttpExecutor:
    """Sends requests through a rate limiter with retries, backoff and 429 handling.

    * every attempt (including retries) first acquires a limiter token;
    * HTTP 429 penalises the shared limiter, so *all* workers pause for the
      ``Retry-After`` period (or an exponential cooldown) and continue slower;
    * timeouts, connection errors and ``retry_statuses`` are retried with
      exponential backoff and jitter, up to ``policy.max_retries`` times;
    * any other response is returned to the caller to interpret.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: AsyncRateLimiter,
        policy: RetryPolicy,
        *,
        retry_statuses: Collection[int] = RETRYABLE_STATUS_CODES,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
        name: str = "http",
    ) -> None:
        self.client = client
        self.limiter = limiter
        self.policy = policy
        self.retry_statuses = frozenset(retry_statuses)
        self.name = name
        self.stats = RequestStats()
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._warned_header = False

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        attempt = 0
        while True:
            await self.limiter.acquire()
            self.stats.requests += 1
            status: int | None = None
            try:
                response = await self.client.request(method, url, headers=headers, json=json)
            except httpx.TimeoutException as exc:
                self.stats.timeouts += 1
                error = f"timeout ({type(exc).__name__})"
                delay = self.policy.backoff(attempt, self._rng)
            except httpx.TransportError as exc:
                self.stats.network_errors += 1
                error = f"network error ({type(exc).__name__})"
                delay = self.policy.backoff(attempt, self._rng)
            else:
                status = response.status_code
                self.stats.status_counts[status] = self.stats.status_counts.get(status, 0) + 1
                if status == 429:
                    self.stats.rate_limited += 1
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    cooldown = self.policy.rate_limited_delay(attempt, retry_after, self._rng)
                    self.limiter.penalize(cooldown)
                    logger.warning(
                        "%s: HTTP 429 from %s — pausing all requests for %.1fs (Retry-After: %s)",
                        self.name,
                        _where(url),
                        cooldown,
                        "none" if retry_after is None else f"{retry_after:g}s",
                    )
                    if attempt >= self.policy.max_retries:
                        raise RateLimited(
                            f"HTTP 429 Too Many Requests (gave up after {attempt + 1} attempts)",
                            retry_after=retry_after,
                            attempts=attempt + 1,
                        )
                    attempt += 1
                    self.stats.retries += 1
                    continue  # the limiter now enforces the cooldown for every worker
                self._observe_rate_limit_header(response)
                if status in self.retry_statuses:
                    self.stats.server_errors += 1
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    delay = max(retry_after or 0.0, self.policy.backoff(attempt, self._rng))
                    error = f"HTTP {status}"
                    if status == 503:
                        self.limiter.slow_down()
                else:
                    self.limiter.record_success()
                    return response

            if attempt >= self.policy.max_retries:
                raise RequestFailed(
                    f"{error} (gave up after {attempt + 1} attempts)",
                    status_code=status,
                    attempts=attempt + 1,
                )
            attempt += 1
            self.stats.retries += 1
            logger.info(
                "%s: %s from %s — retry %d/%d in %.1fs",
                self.name,
                error,
                _where(url),
                attempt,
                self.policy.max_retries,
                delay,
            )
            await self._sleep(delay)

    def _observe_rate_limit_header(self, response: httpx.Response) -> None:
        value = response.headers.get(RATE_LIMIT_HEADER)
        if value and value.strip().upper() != "UNDER_LIMIT":
            self.limiter.slow_down(0.8)
            if not self._warned_header:
                self._warned_header = True
                logger.warning(
                    "%s: server reports rate-limit state %r — slowing down", self.name, value
                )
