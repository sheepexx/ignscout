import asyncio

import pytest

from minecraft_finder.rate_limit import AsyncRateLimiter


def test_requests_are_spaced_by_rate(clock):
    async def go():
        limiter = AsyncRateLimiter(2.0, burst=1, clock=clock, sleep=clock.sleep)
        times = []
        for _ in range(5):
            await limiter.acquire()
            times.append(clock.now)
        return times

    times = asyncio.run(go())
    assert times[0] == 1000.0  # first request is immediate
    assert [round(b - a, 6) for a, b in zip(times, times[1:])] == [0.5] * 4


def test_burst(clock):
    async def go():
        limiter = AsyncRateLimiter(1.0, burst=3, clock=clock, sleep=clock.sleep)
        times = []
        for _ in range(4):
            await limiter.acquire()
            times.append(clock.now)
        return times

    assert asyncio.run(go()) == [1000.0, 1000.0, 1000.0, 1001.0]


def test_concurrent_callers_share_the_budget(clock):
    async def go():
        limiter = AsyncRateLimiter(4.0, burst=1, clock=clock, sleep=clock.sleep)
        times = []

        async def worker():
            await limiter.acquire()
            times.append(clock.now)

        await asyncio.gather(*(worker() for _ in range(8)))
        return sorted(times)

    times = asyncio.run(go())
    assert times[-1] - times[0] == pytest.approx(7 * 0.25)
    assert all(b - a == pytest.approx(0.25) for a, b in zip(times, times[1:]))


def test_penalize_pauses_everyone_and_halves_rate(clock):
    async def go():
        limiter = AsyncRateLimiter(2.0, burst=1, clock=clock, sleep=clock.sleep)
        await limiter.acquire()
        limiter.penalize(10.0)
        assert limiter.rate == pytest.approx(1.0)
        assert limiter.cooldown_remaining == pytest.approx(10.0)
        assert limiter.is_throttled
        await limiter.acquire()
        return clock.now

    # 10 s cooldown, then one full interval at the reduced rate (no burst after a 429).
    assert asyncio.run(go()) == pytest.approx(1011.0)


def test_penalize_respects_min_rate(clock):
    limiter = AsyncRateLimiter(1.0, min_rate=0.2, clock=clock, sleep=clock.sleep)
    for _ in range(10):
        limiter.penalize()
    assert limiter.rate == pytest.approx(0.2)
    assert limiter.penalties == 10


def test_recovery_is_gradual_and_capped(clock):
    limiter = AsyncRateLimiter(2.0, recovery_after=5, recovery_step=0.25, clock=clock, sleep=clock.sleep)
    limiter.penalize()
    assert limiter.rate == pytest.approx(1.0)
    for _ in range(5):
        limiter.record_success()
    assert limiter.rate == pytest.approx(1.5)
    for _ in range(20):
        limiter.record_success()
    assert limiter.rate == pytest.approx(2.0)
    assert not limiter.is_throttled


def test_slow_down(clock):
    limiter = AsyncRateLimiter(1.0, clock=clock, sleep=clock.sleep)
    limiter.slow_down(0.5)
    assert limiter.rate == pytest.approx(0.5)


def test_rejects_non_positive_rate():
    with pytest.raises(ValueError):
        AsyncRateLimiter(0)


def test_cancelled_waiter_releases_lock(clock):
    async def go():
        limiter = AsyncRateLimiter(1.0, clock=clock, sleep=asyncio.sleep)
        await limiter.acquire()
        waiter = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        clock.now += 1.0
        await asyncio.wait_for(limiter.acquire(), timeout=1.0)

    asyncio.run(go())
