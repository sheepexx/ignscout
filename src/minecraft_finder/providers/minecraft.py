"""Primary provider: the official Mojang / Minecraft Services profile endpoints.

Endpoint research (verified 2026-10-02 against the live API and minecraft.wiki "Mojang API"):

``GET  api.minecraftservices.com/minecraft/profile/lookup/name/{name}``
    200 ``{"id", "name"}`` when a profile uses the name;
    404 ``{"path", "errorMessage": "Couldn't find any profile with name …"}`` otherwise.
``POST api.minecraftservices.com/minecraft/profile/lookup/bulk/byname``
    JSON array of at most 10 names -> 200 with the profiles that exist (case-insensitive
    match, canonical spelling returned, missing names simply absent); 400 for >10 names.
``GET  api.minecraftservices.com/minecraft/profile/name/{name}/available``
    Requires a Bearer token (401 without) -> ``{"status": "AVAILABLE" | "DUPLICATE" | "NOT_ALLOWED"}``.

Notes:

* ``api.mojang.com/users/profiles/minecraft/{name}`` still answers but is the legacy host
  (documented to return sporadic 403s), so it is not used by default.
* The name-history endpoint ``/user/profiles/{uuid}/names`` was removed on 2022-09-13.
* Documented limits: about 200 requests per 2 minutes per IP (IPv6 bucketed per /56); the
  availability endpoint allows 20 requests per 5 minutes per account. Responses carry an
  ``x-minecraft-rate-limit-result`` header (``UNDER_LIMIT`` when fine).
* A 404 from the lookup only proves that *no profile currently uses the name*. The name may
  still be unclaimable (inside the 37-day hold after a rename, or blocked), so such results
  are AVAILABLE with ``confidence=unverified`` unless the token-backed check confirms them.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from ..http_client import HttpExecutor, RequestFailed, RequestStats
from ..models import CheckResult, Confidence, Status
from ..rate_limit import AsyncRateLimiter, Clock, Sleep
from ..retry import RetryPolicy
from .base import Provider, ProviderInfo, ProviderRole, ProviderSelfTestError, make_client

if TYPE_CHECKING:
    from ..config import AppConfig, MinecraftProviderConfig, ScannerConfig

logger = logging.getLogger(__name__)

#: Mojang documents ~200 requests per 2 minutes per IP; never configure more than that.
DOCUMENTED_IP_LIMIT_RPS = 200 / 120
#: The availability endpoint allows 20 requests per 5 minutes per account.
DOCUMENTED_AVAILABILITY_LIMIT_RPS = 20 / 300
BULK_LIMIT = 10
#: A long-lived, well-known profile used to prove the lookup endpoint still behaves.
CANARY_USERNAME = "Notch"

UNVERIFIED_DETAIL = (
    "No Minecraft profile uses this name. Claimability is not verified — it may be reserved "
    "(37-day hold after a rename) or blocked; use --verify with an access token to confirm."
)


class UnexpectedResponse(Exception):
    pass


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _parse_profile(payload: Any) -> dict[str, str] | None:
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("id"), str)
        and isinstance(payload.get("name"), str)
        and payload["name"]
    ):
        return {"id": payload["id"], "name": payload["name"]}
    return None


def _parse_bulk(response: httpx.Response) -> dict[str, dict[str, str]]:
    if response.status_code != 200:
        raise UnexpectedResponse(f"bulk lookup returned HTTP {response.status_code}")
    payload = _json(response)
    if not isinstance(payload, list):
        raise UnexpectedResponse("bulk lookup returned a non-list body")
    found: dict[str, dict[str, str]] = {}
    for item in payload:
        profile = _parse_profile(item)
        if profile is None:
            raise UnexpectedResponse("bulk lookup returned a malformed profile")
        found[profile["name"].lower()] = profile
    return found


def _is_not_found_body(response: httpx.Response) -> bool:
    """A genuine "no such profile" 404 carries Mojang's JSON error object.

    Anything else (HTML, empty body) suggests the endpoint moved, which must never be
    misread as "every name is available".
    """
    payload = _json(response)
    return isinstance(payload, dict) and any(k in payload for k in ("errorMessage", "error", "path"))


def _summary(response: httpx.Response) -> str:
    payload = _json(response)
    if isinstance(payload, dict):
        for key in ("errorMessage", "error", "errorType"):
            if isinstance(payload.get(key), str):
                return payload[key][:160]
    return response.text[:160].strip() or f"HTTP {response.status_code}"


class MinecraftProvider(Provider):
    name = "minecraft"
    display_name = "Minecraft"
    role = ProviderRole.PRIMARY
    max_batch_size = BULK_LIMIT

    def __init__(
        self,
        config: MinecraftProviderConfig,
        scanner: ScannerConfig,
        *,
        client: httpx.AsyncClient | None = None,
        token: str | None = None,
        verify: bool = False,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.requested_rps = scanner.requests_per_second
        self.effective_rps = min(scanner.requests_per_second, DOCUMENTED_IP_LIMIT_RPS)
        self._owns_client = client is None
        self._client = client if client is not None else make_client(scanner)
        rng = rng or random.Random()
        policy = RetryPolicy(
            max_retries=scanner.max_retries,
            base_delay=scanner.backoff_base,
            max_delay=scanner.backoff_max,
            rate_limit_delay=scanner.rate_limit_cooldown,
        )
        self._limiter = AsyncRateLimiter(
            self.effective_rps, burst=scanner.burst, clock=clock, sleep=sleep, name="minecraft"
        )
        self._http = HttpExecutor(
            self._client, self._limiter, policy, sleep=sleep, rng=rng, name="minecraft"
        )
        verify_rps = min(config.verify_requests_per_minute / 60.0, DOCUMENTED_AVAILABILITY_LIMIT_RPS)
        self._verify_limiter = AsyncRateLimiter(
            verify_rps, burst=1, clock=clock, sleep=sleep, name="minecraft-verify"
        )
        self._verify_http = HttpExecutor(
            self._client, self._verify_limiter, policy, sleep=sleep, rng=rng, name="minecraft-verify"
        )
        self._token = token if verify else None
        self.verify_requested = verify
        self.verify_disabled_reason: str | None = None
        if verify and not token:
            self.verify_disabled_reason = "no access token provided"

    @classmethod
    def info(cls, config: AppConfig) -> ProviderInfo:
        mc = config.providers.minecraft
        return ProviderInfo(
            name=cls.name,
            display_name=cls.display_name,
            role=cls.role,
            description=(
                "Official Mojang profile lookups (bulk, 10 names per request). "
                "Optional token-backed availability check confirms claimability."
            ),
            endpoints=(mc.bulk_url, mc.lookup_url, f"{mc.availability_url} (token)"),
            rate_limit="≈200 requests / 2 min per IP\navailability check:\n20 / 5 min per account",
        )

    # -- state -------------------------------------------------------------------------------

    @property
    def verifying(self) -> bool:
        return self._token is not None and self.verify_disabled_reason is None

    @property
    def limiter(self) -> AsyncRateLimiter:
        return self._limiter

    @property
    def request_stats(self) -> RequestStats:
        return RequestStats.merged(self._http.stats, self._verify_http.stats)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _disable_verification(self, reason: str) -> None:
        if self.verify_disabled_reason is None:
            self.verify_disabled_reason = reason
            logger.warning("availability verification disabled: %s", reason)

    # -- self-test ---------------------------------------------------------------------------

    async def self_test(self) -> None:
        url = self.config.lookup_url.format(name=quote(CANARY_USERNAME, safe=""))
        try:
            response = await self._http.request("GET", url)
        except RequestFailed as exc:
            raise ProviderSelfTestError(f"self-test request failed: {exc}") from exc
        profile = _parse_profile(_json(response)) if response.status_code == 200 else None
        if profile is None or profile["name"].lower() != CANARY_USERNAME.lower():
            raise ProviderSelfTestError(
                f"looking up the well-known profile {CANARY_USERNAME!r} returned HTTP "
                f"{response.status_code} instead of a profile. The endpoint may have changed; "
                "refusing to scan so that 'not found' is not misread as 'available'."
            )

    # -- checks ------------------------------------------------------------------------------

    async def check(self, username: str) -> CheckResult:
        return (await self.check_many([username]))[0]

    async def check_many(self, usernames: Sequence[str]) -> list[CheckResult]:
        names = list(usernames)
        if len(names) > BULK_LIMIT:
            results: list[CheckResult] = []
            for start in range(0, len(names), BULK_LIMIT):
                results.extend(await self.check_many(names[start : start + BULK_LIMIT]))
            return results
        if not names:
            return []
        if self.config.use_bulk and len(names) > 1:
            results = await self._bulk(names)
        else:
            results = [await self._single(name) for name in names]
        if self.verifying:
            results = [
                await self._verify(result) if result.status is Status.AVAILABLE else result
                for result in results
            ]
        return results

    async def _bulk(self, names: list[str]) -> list[CheckResult]:
        try:
            response = await self._http.request("POST", self.config.bulk_url, json=names)
        except RequestFailed as exc:
            return [self._error(name, str(exc)) for name in names]
        if response.status_code == 400:
            logger.warning(
                "bulk lookup rejected (HTTP 400: %s); falling back to single lookups",
                _summary(response),
            )
            return [await self._single(name) for name in names]
        try:
            found = _parse_bulk(response)
        except UnexpectedResponse as exc:
            return [self._error(name, str(exc)) for name in names]
        return [self._from_profile(name, found.get(name.lower())) for name in names]

    async def _single(self, name: str) -> CheckResult:
        url = self.config.lookup_url.format(name=quote(name, safe=""))
        try:
            response = await self._http.request("GET", url)
        except RequestFailed as exc:
            return self._error(name, str(exc))
        status = response.status_code
        if status == 200:
            profile = _parse_profile(_json(response))
            if profile is None:
                return self._error(name, "unexpected response body for HTTP 200")
            return self._from_profile(name, profile)
        if status == 404:
            if not _is_not_found_body(response):
                return self._error(name, "unexpected HTTP 404 body (endpoint may have moved)")
            return self._from_profile(name, None)
        if status == 204:  # historical "no such profile" answer of the legacy endpoint
            return self._from_profile(name, None)
        if status == 400:
            return self._result(
                name,
                Status.UNKNOWN,
                detail=f"The API rejected the name (HTTP 400: {_summary(response)}).",
            )
        return self._error(name, f"unexpected HTTP {status}: {_summary(response)}")

    def _from_profile(self, name: str, profile: dict[str, str] | None) -> CheckResult:
        if profile is None:
            return self._result(
                name, Status.AVAILABLE, confidence=Confidence.UNVERIFIED, detail=UNVERIFIED_DETAIL
            )
        return self._result(
            name,
            Status.TAKEN,
            confidence=Confidence.CONFIRMED,
            uuid=profile["id"],
            display_name=profile["name"],
        )

    async def verify_name(self, username: str) -> CheckResult:
        """Mojang's authoritative answer for one name (needs the token).

        AVAILABLE -> claimable now (confirmed); NOT_ALLOWED -> BLOCKED; DUPLICATE -> either taken
        or held after a rename, told apart with one profile lookup. If the check could not be
        completed, the result stays AVAILABLE/unverified (see ``verify_disabled_reason``).
        """
        unverified = self._result(username, Status.AVAILABLE, confidence=Confidence.UNVERIFIED, detail=UNVERIFIED_DETAIL)
        result = await self._verify(unverified)
        if result.status is Status.SOON:  # DUPLICATE: maybe someone claimed it since the scan
            current = await self._single(username)
            if current.status in (Status.TAKEN, Status.ERROR):
                return current if current.status is Status.TAKEN else result
        return result

    async def _verify(self, result: CheckResult) -> CheckResult:
        """Ask the token-backed availability endpoint whether an unclaimed name is claimable."""
        if not self.verifying:
            return result
        url = self.config.availability_url.format(name=quote(result.username, safe=""))
        try:
            response = await self._verify_http.request(
                "GET", url, headers={"Authorization": f"Bearer {self._token}"}
            )
        except RequestFailed as exc:
            logger.warning("availability check for %s failed: %s", result.username, exc)
            return result.evolve(detail=f"{UNVERIFIED_DETAIL} (Verification failed: {exc}.)")
        if response.status_code in (401, 403):
            self._disable_verification(
                f"access token rejected (HTTP {response.status_code}); it may have expired"
            )
            return result
        if response.status_code != 200:
            return result.evolve(
                detail=f"{UNVERIFIED_DETAIL} (Verification returned HTTP {response.status_code}.)"
            )
        payload = _json(response)
        state = payload.get("status") if isinstance(payload, dict) else None
        if state == "AVAILABLE":
            return result.evolve(
                confidence=Confidence.CONFIRMED,
                detail="Mojang's name-availability check reports this name as claimable.",
            )
        if state == "DUPLICATE":
            return result.evolve(
                status=Status.SOON,
                confidence=Confidence.NONE,
                available_at=None,
                detail=(
                    "No profile uses this name, but Mojang reports it as not claimable "
                    "(DUPLICATE). It is either inside the 37-day hold after a rename or locked "
                    "for longer (common for short names); official data cannot tell which, or "
                    "when it will be released."
                ),
            )
        if state == "NOT_ALLOWED":
            return result.evolve(
                status=Status.BLOCKED,
                confidence=Confidence.CONFIRMED,
                detail="Mojang does not allow this name (NOT_ALLOWED).",
            )
        return result.evolve(
            status=Status.UNKNOWN,
            confidence=Confidence.NONE,
            detail=f"Unrecognised availability status {state!r}.",
        )
