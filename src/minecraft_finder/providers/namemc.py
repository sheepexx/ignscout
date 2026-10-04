"""Optional, best-effort NameMC enrichment (disabled by default).

NameMC is a third-party site, not an API. This provider exists only to spot
names that Mojang reports as unclaimed but NameMC shows as "Available Later",
which yields an *estimated* release time. Guard rails:

* off unless ``providers.namemc.enabled = true`` or ``--namemc`` is given;
* fetches ``/robots.txt`` first and refuses to run if ``/search`` is disallowed;
  honours a ``Crawl-delay`` that is longer than the configured interval;
* at most one request every ``min_interval_seconds`` (minimum 5 s, default 10 s);
* never tries to get past Cloudflare, CAPTCHAs or other anti-bot measures — an
  anti-bot challenge disables the provider for the rest of the session;
* disables itself after ``max_consecutive_failures`` failures in a row;
* the page parser is deliberately strict: an unrecognised layout yields UNKNOWN.

Review NameMC's terms of service before enabling this integration.
"""

from __future__ import annotations

import asyncio
import enum
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import quote
from urllib.robotparser import RobotFileParser

import httpx

from ..availability import ReleaseKind, validate_reported_release
from ..http_client import HttpExecutor, RateLimited, RequestFailed, RequestStats
from ..models import CheckResult, Confidence, Status
from ..rate_limit import AsyncRateLimiter, Clock, Sleep
from ..retry import RetryPolicy
from ..timeutil import ensure_utc
from .base import Provider, ProviderInfo, ProviderRole, make_client

if TYPE_CHECKING:
    from ..config import AppConfig, NameMCProviderConfig, ScannerConfig

logger = logging.getLogger(__name__)

SEARCH_PATH = "/search?q={name}"
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>")
_STATUS_RE = re.compile(r"\bStatus\b\s*:?\s*(Available\s+Later|Unavailable|Available|Invalid)", re.I)
_TIME_RE = re.compile(r"<time\b[^>]*\bdatetime\s*=\s*[\"']([^\"']+)[\"']", re.I)
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-chl",
    "challenge-platform",
    "attention required",
    "cf-browser-verification",
    "captcha",
)


class NameMCStatus(enum.StrEnum):
    AVAILABLE = "available"
    AVAILABLE_LATER = "available_later"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class NameMCSearch:
    status: NameMCStatus | None
    available_at: datetime | None = None


def parse_search_page(page: str) -> NameMCSearch:
    """Extract the status (and release time for "Available Later") from a search page."""
    text = html.unescape(_TAG_RE.sub(" ", _SCRIPT_RE.sub(" ", page)))
    match = _STATUS_RE.search(" ".join(text.split()))
    if match is None:
        return NameMCSearch(None)
    label = " ".join(match.group(1).lower().split())
    status = {
        "available later": NameMCStatus.AVAILABLE_LATER,
        "unavailable": NameMCStatus.UNAVAILABLE,
        "available": NameMCStatus.AVAILABLE,
        "invalid": NameMCStatus.INVALID,
    }[label]
    available_at = None
    if status is NameMCStatus.AVAILABLE_LATER:
        time_match = _TIME_RE.search(page)
        if time_match:
            try:
                available_at = ensure_utc(datetime.fromisoformat(time_match.group(1).strip()))
            except ValueError:
                available_at = None
    return NameMCSearch(status, available_at)


def is_challenge(response: httpx.Response) -> bool:
    """Detect Cloudflare-style interstitials. These are respected, never bypassed."""
    if response.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if response.status_code in (403, 429, 503):
        body = response.text[:4000].lower()
        return any(marker in body for marker in _CHALLENGE_MARKERS)
    return False


class NameMCProvider(Provider):
    name = "namemc"
    display_name = "NameMC"
    role = ProviderRole.ENRICHMENT
    max_batch_size = 1

    def __init__(
        self,
        config: NameMCProviderConfig,
        scanner: ScannerConfig,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        super().__init__()
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.user_agent = scanner.user_agent
        self._owns_client = client is None
        self._client = client if client is not None else make_client(scanner, accept="text/html")
        self._sleep = sleep
        self._clock = clock
        self._interval = max(config.min_interval_seconds, 5.0)
        self._limiter = AsyncRateLimiter(1.0 / self._interval, burst=1, clock=clock, sleep=sleep, name="namemc")
        policy = RetryPolicy(max_retries=1, base_delay=self._interval, max_delay=self._interval * 4)
        # 403/503 are not retried: on this site they usually mean an anti-bot challenge.
        self._http = HttpExecutor(
            self._client, self._limiter, policy, retry_statuses={500, 502, 504}, sleep=sleep, name="namemc"
        )
        self._failures = 0
        self._started = False

    @classmethod
    def info(cls, config: AppConfig) -> ProviderInfo:
        nm = config.providers.namemc
        return ProviderInfo(
            name=cls.name,
            display_name=cls.display_name,
            role=cls.role,
            description=(
                "Optional, experimental. Only consulted for names Mojang reports as unclaimed, to "
                "find 'Available Later' estimates. Respects robots.txt; disables itself on anti-bot "
                "challenges."
            ),
            endpoints=(f"{nm.base_url.rstrip('/')}/robots.txt", f"{nm.base_url.rstrip('/')}{SEARCH_PATH}"),
            rate_limit=f"1 request / {max(nm.min_interval_seconds, 5.0):g}s (self-imposed)",
        )

    @property
    def limiter(self) -> AsyncRateLimiter:
        return self._limiter

    @property
    def request_stats(self) -> RequestStats:
        return self._http.stats

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self._check_robots()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _check_robots(self) -> None:
        try:
            response = await self._http.request("GET", f"{self.base_url}/robots.txt")
        except RequestFailed as exc:
            self.disable(f"could not fetch robots.txt ({exc}); not crawling without permission")
            return
        if is_challenge(response):
            self.disable("robots.txt request hit an anti-bot challenge; not attempting to bypass it")
            return
        if response.status_code in (401, 403):
            self.disable(f"robots.txt access denied (HTTP {response.status_code}); treating site as off-limits")
            return
        if response.status_code >= 500:
            self.disable(f"robots.txt unavailable (HTTP {response.status_code}); permission unknown")
            return
        if response.status_code == 404:
            return  # no robots.txt: no restrictions declared
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        probe = f"{self.base_url}{SEARCH_PATH.format(name='example')}"
        if not parser.can_fetch(self.user_agent, probe):
            self.disable("robots.txt disallows /search for this user agent")
            return
        delay = parser.crawl_delay(self.user_agent)
        if delay and float(delay) > self._interval:
            self._interval = float(delay)
            self._limiter = AsyncRateLimiter(
                1.0 / self._interval, burst=1, clock=self._clock, sleep=self._sleep, name="namemc"
            )
            self._http.limiter = self._limiter
            logger.info("namemc: honouring robots.txt Crawl-delay of %ss", delay)

    def _failure(self, reason: str) -> None:
        self._failures += 1
        logger.info("namemc: failure %d/%d: %s", self._failures, self.config.max_consecutive_failures, reason)
        if self._failures >= self.config.max_consecutive_failures:
            self.disable(f"{self._failures} consecutive failures (last: {reason})")

    def _unknown(self, username: str, detail: str) -> CheckResult:
        return self._result(username, Status.UNKNOWN, detail=detail)

    async def check(self, username: str) -> CheckResult:
        if not self._started:
            await self.start()
        if not self.enabled:
            return self._unknown(username, f"NameMC disabled: {self.disabled_reason}")
        url = f"{self.base_url}{SEARCH_PATH.format(name=quote(username, safe=''))}"
        try:
            response = await self._http.request("GET", url)
        except RateLimited:
            self._failure("rate limited (HTTP 429)")
            return self._unknown(username, "NameMC rate-limited the request")
        except RequestFailed as exc:
            self._failure(str(exc))
            return self._unknown(username, f"NameMC request failed: {exc}")
        if is_challenge(response):
            self.disable("anti-bot challenge detected; integration disabled (no bypass is attempted)")
            return self._unknown(username, "NameMC presented an anti-bot challenge")
        if response.status_code != 200:
            self._failure(f"HTTP {response.status_code}")
            return self._unknown(username, f"NameMC returned HTTP {response.status_code}")

        parsed = parse_search_page(response.text)
        if parsed.status is None:
            self._failure("unrecognised page layout")
            return self._unknown(username, "NameMC page layout not recognised")
        self._failures = 0

        match parsed.status:
            case NameMCStatus.AVAILABLE:
                return self._result(
                    username, Status.AVAILABLE, confidence=Confidence.UNVERIFIED, detail="NameMC shows 'Available'."
                )
            case NameMCStatus.UNAVAILABLE:
                return self._result(username, Status.TAKEN, detail="NameMC shows 'Unavailable'.")
            case NameMCStatus.INVALID:
                return self._unknown(username, "NameMC shows 'Invalid'.")
        estimate = validate_reported_release(parsed.available_at, source="NameMC")
        if estimate.kind is ReleaseKind.ESTIMATED:
            return self._result(
                username,
                Status.SOON,
                confidence=Confidence.ESTIMATED,
                available_at=estimate.at,
                detail="NameMC shows 'Available Later' (estimate).",
            )
        return self._result(username, Status.SOON, detail=estimate.basis)
