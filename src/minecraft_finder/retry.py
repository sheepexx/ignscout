"""Retry policy: exponential backoff with jitter and ``Retry-After`` parsing."""

from __future__ import annotations

import email.utils
import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime

from .timeutil import utcnow

#: Temporary failures worth retrying (request timeout and transient server errors).
RETRYABLE_STATUS_CODES = frozenset({408, 500, 502, 503, 504})
#: Upper bound for a single Retry-After value we are willing to schedule.
MAX_RETRY_AFTER = 3600.0
#: Upper bound for computed 429 cooldowns when the server sends no Retry-After.
MAX_RATE_LIMIT_DELAY = 300.0


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date) into seconds."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or utcnow())).total_seconds()
    if not math.isfinite(seconds):
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 4
    base_delay: float = 1.0
    max_delay: float = 60.0
    rate_limit_delay: float = 30.0
    jitter: float = 0.5

    def backoff(self, attempt: int, rng: random.Random | None = None) -> float:
        """Delay before retry number ``attempt + 1`` after a timeout or 5xx.

        Exponential (``base * 2**attempt``, capped at ``max_delay``) with jitter that
        only shortens the delay, spreading retries of concurrent workers apart.
        """
        rng = rng or random
        raw = min(self.max_delay, self.base_delay * (2**attempt))
        return raw * (1.0 - self.jitter * rng.random())

    def rate_limited_delay(
        self, attempt: int, retry_after: float | None, rng: random.Random | None = None
    ) -> float:
        """Cooldown after HTTP 429. Never shorter than the server's ``Retry-After``."""
        rng = rng or random
        if retry_after is not None:
            return retry_after + rng.random() * 0.5
        raw = min(MAX_RATE_LIMIT_DELAY, self.rate_limit_delay * (2**attempt))
        return raw * (1.0 + 0.25 * rng.random())
