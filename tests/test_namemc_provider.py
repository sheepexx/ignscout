"""NameMC enrichment: robots.txt, anti-bot handling and strict parsing (all mocked)."""

import asyncio
from datetime import timedelta

import httpx
import pytest

from minecraft_finder.config import AppConfig
from minecraft_finder.models import Confidence, Status
from minecraft_finder.providers.namemc import NameMCProvider, NameMCStatus, parse_search_page
from minecraft_finder.timeutil import utcnow

from .conftest import mock_client

REAL_ROBOTS = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /minecraft-names?\nDisallow: /i/emoji/\n"


def search_page(status: str, iso: str | None = None) -> str:
    when = f'<div><strong>Time of Availability</strong>: <time datetime="{iso}">soon</time></div>' if iso else ""
    return (
        "<html><head><script>var x = 'Status: Available';</script></head><body>"
        f'<div class="card"><div><strong>Status</strong>:</div><div class="text-right">{status}</div></div>{when}'
        "</body></html>"
    )


def test_parse_available_later():
    at = utcnow() + timedelta(days=3)
    parsed = parse_search_page(search_page("Available Later", at.isoformat()))
    assert parsed.status is NameMCStatus.AVAILABLE_LATER
    assert abs((parsed.available_at - at).total_seconds()) < 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [("Unavailable", NameMCStatus.UNAVAILABLE), ("Available", NameMCStatus.AVAILABLE), ("Invalid", NameMCStatus.INVALID)],
)
def test_parse_statuses(status, expected):
    assert parse_search_page(search_page(status)).status is expected


def test_parse_unknown_layout_and_scripts_are_ignored():
    assert parse_search_page("<html><script>Status: Available</script><p>nothing</p></html>").status is None


def make(routes, clock, **overrides):
    requests = []

    def handler(request):
        requests.append((request.url.path, clock.now))
        response = routes(request)
        return response

    config = AppConfig()
    config.providers.namemc.enabled = True
    config.providers.namemc.min_interval_seconds = 5
    for key, value in overrides.items():
        setattr(config.providers.namemc, key, value)
    provider = NameMCProvider(config.providers.namemc, config.scanner, client=mock_client(handler), sleep=clock.sleep, clock=clock)
    return provider, requests


def run(provider, names):
    async def go():
        async with provider:
            return [await provider.check(name) for name in names]

    return asyncio.run(go())


def test_robots_disallow_disables_without_searching(clock):
    provider, requests = make(lambda r: httpx.Response(200, text="User-agent: *\nDisallow: /search\n"), clock)
    [result] = run(provider, ["glacier"])
    assert not provider.enabled
    assert "robots.txt disallows" in provider.disabled_reason
    assert result.status is Status.UNKNOWN
    assert [path for path, _ in requests] == ["/robots.txt"]


def test_available_later_becomes_estimated_soon(clock):
    at = utcnow() + timedelta(days=3)

    def routes(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=REAL_ROBOTS)
        return httpx.Response(200, text=search_page("Available Later", at.isoformat()))

    provider, _ = make(routes, clock)
    [result] = run(provider, ["glacier"])
    assert result.status is Status.SOON
    assert result.confidence is Confidence.ESTIMATED
    assert result.available_at is not None


def test_release_beyond_the_hold_window_is_not_trusted(clock):
    at = utcnow() + timedelta(days=90)

    def routes(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=REAL_ROBOTS)
        return httpx.Response(200, text=search_page("Available Later", at.isoformat()))

    provider, _ = make(routes, clock)
    [result] = run(provider, ["glacier"])
    assert result.status is Status.SOON
    assert result.available_at is None
    assert "not trusted" in result.detail


def test_challenge_disables_and_is_never_retried(clock):
    def routes(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=REAL_ROBOTS)
        return httpx.Response(403, text="<title>Just a moment...</title>", headers={"cf-mitigated": "challenge"})

    provider, requests = make(routes, clock)
    results = run(provider, ["glacier", "lantern"])
    assert all(r.status is Status.UNKNOWN for r in results)
    assert not provider.enabled
    assert "anti-bot" in provider.disabled_reason
    assert [path for path, _ in requests] == ["/robots.txt", "/search"]  # one attempt, then stop


def test_challenge_on_robots_disables(clock):
    provider, _ = make(lambda r: httpx.Response(503, text="Checking your browser... cf-chl"), clock)
    run(provider, ["glacier"])
    assert not provider.enabled


def test_consecutive_failures_disable(clock):
    def routes(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=REAL_ROBOTS)
        return httpx.Response(200, text="<html>redesigned page</html>")

    provider, requests = make(routes, clock, max_consecutive_failures=2)
    run(provider, ["a_one", "a_two", "a_three"])
    assert not provider.enabled
    assert len([p for p, _ in requests if p == "/search"]) == 2


def test_requests_are_spaced_and_crawl_delay_is_honoured(clock):
    def routes(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=REAL_ROBOTS + "Crawl-delay: 30\n")
        return httpx.Response(200, text=search_page("Unavailable"))

    provider, requests = make(routes, clock)
    run(provider, ["glacier", "lantern"])
    searches = [t for p, t in requests if p == "/search"]
    assert searches[1] - searches[0] >= 30
