"""Minecraft provider against mocked Mojang responses (no live traffic)."""

import asyncio
import json
import logging
import random

import httpx
import pytest

from minecraft_finder.config import AppConfig
from minecraft_finder.models import Confidence, Status
from minecraft_finder.providers.base import ProviderSelfTestError
from minecraft_finder.providers.minecraft import (
    DOCUMENTED_AVAILABILITY_LIMIT_RPS,
    DOCUMENTED_IP_LIMIT_RPS,
    MinecraftProvider,
)

from .conftest import mock_client

NOTCH = {"id": "069a79f444e94726a5befca90e38aaf5", "name": "Notch"}
TOKEN = "eyJhbGciOiJIUzI1NiJ9.c2VjcmV0LXBheWxvYWQtdmFsdWU.c2lnbmF0dXJlLXZhbHVlLTEyMw"


def not_found(name: str) -> httpx.Response:
    return httpx.Response(
        404,
        json={"path": f"/minecraft/profile/lookup/name/{name}", "errorMessage": f"Couldn't find any profile with name {name}"},
    )


def make_provider(handler, clock, *, token=None, verify=False, use_bulk=True, max_retries=2, rps=1.0):
    config = AppConfig()
    config.providers.minecraft.use_bulk = use_bulk
    config.scanner.max_retries = max_retries
    config.scanner.requests_per_second = rps
    client = mock_client(handler)
    provider = MinecraftProvider(
        config.providers.minecraft,
        config.scanner,
        client=client,
        token=token,
        verify=verify,
        sleep=clock.sleep,
        clock=clock,
        rng=random.Random(0),
    )
    return provider, client


def run_checks(provider, client, names):
    async def go():
        async with client:
            return await provider.check_many(names)

    return asyncio.run(go())


def test_single_lookup_taken(clock):
    def handler(request):
        assert request.url.path == "/minecraft/profile/lookup/name/notch"
        return httpx.Response(200, json=NOTCH)

    provider, client = make_provider(handler, clock)
    [result] = run_checks(provider, client, ["notch"])
    assert result.status is Status.TAKEN
    assert result.confidence is Confidence.CONFIRMED
    assert result.display_name == "Notch"
    assert result.uuid == NOTCH["id"]


def test_single_lookup_not_found_is_unverified_available(clock):
    provider, client = make_provider(lambda r: not_found("glacierz"), clock)
    [result] = run_checks(provider, client, ["glacierz"])
    assert result.status is Status.AVAILABLE
    assert result.confidence is Confidence.UNVERIFIED
    assert "not verified" in result.detail


def test_unexpected_404_body_is_an_error_not_available(clock):
    provider, client = make_provider(lambda r: httpx.Response(404, text="<html>Not Found</html>"), clock)
    [result] = run_checks(provider, client, ["glacierz"])
    assert result.status is Status.ERROR
    assert "endpoint may have moved" in result.last_error


def test_bulk_lookup_maps_case_insensitively_in_one_request(clock):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert json.loads(request.content) == ["glacier", "lanternx", "orchardz"]
        return httpx.Response(200, json=[{"id": "a" * 32, "name": "Glacier"}])

    provider, client = make_provider(handler, clock)
    results = run_checks(provider, client, ["glacier", "lanternx", "orchardz"])
    assert [r.status for r in results] == [Status.TAKEN, Status.AVAILABLE, Status.AVAILABLE]
    assert results[0].display_name == "Glacier"
    assert len(requests) == 1


def test_bulk_400_falls_back_to_single_lookups(clock):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(400, json={"errorMessage": "size must be between 1 and 10"})
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=NOTCH) if name == "notch" else not_found(name)

    provider, client = make_provider(handler, clock)
    results = run_checks(provider, client, ["notch", "glacierz"])
    assert [r.status for r in results] == [Status.TAKEN, Status.AVAILABLE]


def test_bulk_429_is_retried_after_retry_after(clock):
    calls = []

    def handler(request):
        calls.append(clock.now)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200, json=[])

    provider, client = make_provider(handler, clock)
    results = run_checks(provider, client, ["glacierz", "lanternz"])
    assert all(r.status is Status.AVAILABLE for r in results)
    assert calls[1] - calls[0] >= 5.0
    assert provider.limiter.penalties == 1
    assert provider.request_stats.rate_limited == 1


def test_persistent_429_yields_error_results(clock):
    provider, client = make_provider(lambda r: httpx.Response(429, headers={"Retry-After": "2"}), clock, max_retries=1)
    results = run_checks(provider, client, ["glacierz", "lanternz"])
    assert all(r.status is Status.ERROR for r in results)
    assert "429" in results[0].last_error
    assert provider.limiter.rate < 1.0


def test_more_than_ten_names_are_split(clock):
    sizes = []

    def handler(request):
        sizes.append(len(json.loads(request.content)))
        return httpx.Response(200, json=[])

    provider, client = make_provider(handler, clock)
    results = run_checks(provider, client, [f"name{i:02d}" for i in range(23)])
    assert sizes == [10, 10, 3]
    assert len(results) == 23


def _verify_handler(state: str, seen_headers: list):
    def handler(request):
        if request.url.path.endswith("/available"):
            seen_headers.append(request.headers.get("Authorization"))
            return httpx.Response(200, json={"status": state})
        return not_found(request.url.path.rsplit("/", 1)[-1])

    return handler


def test_verify_available_confirms_and_sends_bearer_token(clock):
    headers = []
    provider, client = make_provider(_verify_handler("AVAILABLE", headers), clock, token=TOKEN, verify=True)
    [result] = run_checks(provider, client, ["glacierz"])
    assert result.status is Status.AVAILABLE
    assert result.confidence is Confidence.CONFIRMED
    assert headers == [f"Bearer {TOKEN}"]


def test_verify_duplicate_becomes_soon_with_unknown_release(clock):
    provider, client = make_provider(_verify_handler("DUPLICATE", []), clock, token=TOKEN, verify=True)
    [result] = run_checks(provider, client, ["glacierz"])
    assert result.status is Status.SOON
    assert result.available_at is None  # never invent a release date
    assert "37-day" in result.detail


def test_verify_not_allowed_becomes_blocked(clock):
    provider, client = make_provider(_verify_handler("NOT_ALLOWED", []), clock, token=TOKEN, verify=True)
    [result] = run_checks(provider, client, ["glacierz"])
    assert result.status is Status.BLOCKED


def test_verify_401_disables_verification(clock):
    calls = []

    def handler(request):
        if request.url.path.endswith("/available"):
            calls.append(1)
            return httpx.Response(401, json={"path": request.url.path})
        return not_found("x")

    provider, client = make_provider(handler, clock, token=TOKEN, verify=True)
    results = run_checks(provider, client, ["glacierz"])
    assert results[0].status is Status.AVAILABLE
    assert results[0].confidence is Confidence.UNVERIFIED
    assert not provider.verifying
    assert len(calls) == 1


def test_verify_is_spaced_to_the_account_limit(clock):
    times = []

    def handler(request):
        if request.url.path.endswith("/available"):
            times.append(clock.now)
            return httpx.Response(200, json={"status": "AVAILABLE"})
        return httpx.Response(200, json=[])

    provider, client = make_provider(handler, clock, token=TOKEN, verify=True)
    run_checks(provider, client, ["glacierz", "lanternz", "orchardz"])
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(gap >= 1 / DOCUMENTED_AVAILABILITY_LIMIT_RPS - 1e-6 for gap in gaps)  # >= 15 s


def test_request_rate_is_capped_at_documented_limit(clock):
    provider, _ = make_provider(lambda r: httpx.Response(200), clock, rps=10.0)
    assert provider.effective_rps == pytest.approx(DOCUMENTED_IP_LIMIT_RPS)
    assert provider.limiter.rate == pytest.approx(DOCUMENTED_IP_LIMIT_RPS)


def test_self_test(clock):
    ok, client = make_provider(lambda r: httpx.Response(200, json=NOTCH), clock)

    async def run(provider, client):
        async with client:
            await provider.self_test()

    asyncio.run(run(ok, client))
    for response in (not_found("Notch"), httpx.Response(200, text="<html></html>")):
        bad, client = make_provider(lambda r, response=response: response, clock)
        with pytest.raises(ProviderSelfTestError):
            asyncio.run(run(bad, client))


def test_token_is_never_logged(clock, caplog):
    def handler(request):
        if request.url.path.endswith("/available"):
            return httpx.Response(500)
        return not_found("x")

    caplog.set_level(logging.DEBUG)
    provider, client = make_provider(handler, clock, token=TOKEN, verify=True, max_retries=1)
    run_checks(provider, client, ["glacierz"])
    assert caplog.records  # the failure was logged…
    assert TOKEN not in caplog.text  # …without the token
    assert "c2VjcmV0" not in caplog.text
