import asyncio
import random
from datetime import UTC, datetime

import httpx
import pytest

from minecraft_finder.http_client import HttpExecutor, RateLimited, RequestFailed
from minecraft_finder.rate_limit import AsyncRateLimiter
from minecraft_finder.retry import MAX_RETRY_AFTER, RetryPolicy, parse_retry_after

from .conftest import mock_client


@pytest.mark.parametrize(
    ("value", "expected"),
    [("120", 120.0), (" 3 ", 3.0), ("-5", 0.0), ("1.5", 1.5), ("abc", None), ("", None), (None, None), ("999999", MAX_RETRY_AFTER)],
)
def test_parse_retry_after_seconds(value, expected):
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date():
    now = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("Fri, 02 Oct 2026 12:00:30 GMT", now=now) == pytest.approx(30)
    assert parse_retry_after("Fri, 02 Oct 2026 11:00:00 GMT", now=now) == 0.0


def test_backoff_grows_exponentially_and_caps():
    policy = RetryPolicy(base_delay=1.0, max_delay=8.0, jitter=0.0)
    assert [policy.backoff(i) for i in range(6)] == [1, 2, 4, 8, 8, 8]


def test_backoff_jitter_stays_in_bounds():
    policy = RetryPolicy(base_delay=1.0, max_delay=30.0, jitter=0.5)
    rng = random.Random(42)
    for attempt in range(6):
        raw = min(30.0, 2.0**attempt)
        delays = [policy.backoff(attempt, rng) for _ in range(200)]
        assert all(raw * 0.5 <= d <= raw for d in delays)
        assert len({round(d, 6) for d in delays}) > 1  # actually jittered


def test_rate_limited_delay_honours_retry_after():
    policy = RetryPolicy()
    rng = random.Random(1)
    for _ in range(50):
        assert 7.0 <= policy.rate_limited_delay(0, 7.0, rng) <= 7.5


def test_rate_limited_delay_without_header_grows():
    policy = RetryPolicy(rate_limit_delay=30.0)
    rng = random.Random(1)
    assert 30 <= policy.rate_limited_delay(0, None, rng) <= 37.5
    assert 60 <= policy.rate_limited_delay(1, None, rng) <= 75
    assert policy.rate_limited_delay(10, None, rng) <= 300 * 1.25


# -- HttpExecutor ----------------------------------------------------------------------------


def _executor(handler, clock, *, max_retries=3, rate=100.0):
    client = mock_client(handler)
    limiter = AsyncRateLimiter(rate, clock=clock, sleep=clock.sleep)
    policy = RetryPolicy(max_retries=max_retries, base_delay=1.0, max_delay=8.0, rate_limit_delay=30.0)
    return HttpExecutor(client, limiter, policy, sleep=clock.sleep, rng=random.Random(0)), limiter, client


def test_retries_5xx_then_succeeds(clock):
    calls = []

    def handler(request):
        calls.append(clock.now)
        return httpx.Response(503 if len(calls) < 3 else 200, json={"ok": True})

    async def go():
        executor, _, client = _executor(handler, clock)
        async with client:
            response = await executor.request("GET", "https://api.test/x")
        return response, executor

    response, executor = asyncio.run(go())
    assert response.status_code == 200
    assert len(calls) == 3
    assert executor.stats.retries == 2
    assert executor.stats.server_errors == 2
    assert calls[1] - calls[0] >= 0.5  # backoff with jitter: base 1s * (1 - 0.5*rand)


def test_429_with_retry_after_pauses_shared_limiter(clock):
    calls = []

    def handler(request):
        calls.append(clock.now)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=[])

    async def go():
        executor, limiter, client = _executor(handler, clock, rate=10.0)
        async with client:
            response = await executor.request("POST", "https://api.test/bulk", json=["a"])
        return response, executor, limiter

    response, executor, limiter = asyncio.run(go())
    assert response.status_code == 200
    assert calls[1] - calls[0] >= 7.0
    assert executor.stats.rate_limited == 1
    assert limiter.penalties == 1
    assert limiter.rate == pytest.approx(5.0)  # halved after the 429


def test_429_without_retry_after_uses_cooldown(clock):
    calls = []

    def handler(request):
        calls.append(clock.now)
        return httpx.Response(429) if len(calls) == 1 else httpx.Response(200)

    async def go():
        executor, _, client = _executor(handler, clock)
        async with client:
            await executor.request("GET", "https://api.test/x")

    asyncio.run(go())
    assert calls[1] - calls[0] >= 30.0


def test_persistent_429_raises_after_max_retries(clock):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "1"})

    async def go():
        executor, _, client = _executor(handler, clock, max_retries=2)
        async with client:
            with pytest.raises(RateLimited) as info:
                await executor.request("GET", "https://api.test/x")
        return info.value, executor

    error, executor = asyncio.run(go())
    assert len(calls) == 3
    assert error.attempts == 3
    assert error.retry_after == 1.0
    assert executor.stats.rate_limited == 3


def test_timeouts_are_retried_then_reported(clock):
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    async def go():
        executor, _, client = _executor(handler, clock, max_retries=2)
        async with client:
            with pytest.raises(RequestFailed) as info:
                await executor.request("GET", "https://api.test/x")
        return info.value, executor

    error, executor = asyncio.run(go())
    assert "timeout" in str(error)
    assert executor.stats.timeouts == 3


def test_connection_errors_are_retried(clock):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200)

    async def go():
        executor, _, client = _executor(handler, clock)
        async with client:
            return await executor.request("GET", "https://api.test/x")

    assert asyncio.run(go()).status_code == 200


def test_non_retryable_status_is_returned_immediately(clock):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404, json={"errorMessage": "nope"})

    async def go():
        executor, _, client = _executor(handler, clock)
        async with client:
            return await executor.request("GET", "https://api.test/x")

    assert asyncio.run(go()).status_code == 404
    assert len(calls) == 1


def test_rate_limit_header_triggers_slowdown(clock):
    def handler(request):
        return httpx.Response(200, headers={"x-minecraft-rate-limit-result": "OVER_LIMIT"})

    async def go():
        executor, limiter, client = _executor(handler, clock, rate=1.0)
        async with client:
            await executor.request("GET", "https://api.test/x")
        return limiter

    assert asyncio.run(go()).rate < 1.0
