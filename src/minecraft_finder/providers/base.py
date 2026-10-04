"""Provider interface. A provider turns usernames into :class:`CheckResult` objects."""

from __future__ import annotations

import abc
import enum
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import httpx

from ..http_client import RequestStats
from ..models import CheckResult, Status
from ..rate_limit import AsyncRateLimiter

if TYPE_CHECKING:
    from ..config import AppConfig, ScannerConfig

logger = logging.getLogger(__name__)


class ProviderRole(enum.StrEnum):
    PRIMARY = "primary"  # can drive a scan
    ENRICHMENT = "enrichment"  # only consulted for names the primary reports as unclaimed
    OFFLINE = "offline"  # simulation, no network


class ProviderError(Exception):
    pass


class ProviderSelfTestError(ProviderError):
    """The provider's endpoints do not behave as expected; scanning would be unreliable."""


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    name: str
    display_name: str
    role: ProviderRole
    description: str
    endpoints: tuple[str, ...]
    rate_limit: str
    requires_auth: bool = False


def make_client(scanner: ScannerConfig, *, accept: str = "application/json") -> httpx.AsyncClient:
    timeout = httpx.Timeout(scanner.timeout, connect=min(scanner.timeout, 10.0))
    return httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": scanner.user_agent, "Accept": accept},
        follow_redirects=False,
        limits=httpx.Limits(max_connections=max(4, scanner.workers + 2), max_keepalive_connections=8),
    )


class Provider(abc.ABC):
    name: ClassVar[str]
    display_name: ClassVar[str]
    role: ClassVar[ProviderRole] = ProviderRole.PRIMARY
    #: How many names a single check_many() call can resolve with one request.
    max_batch_size: ClassVar[int] = 1

    def __init__(self) -> None:
        self._disabled_reason: str | None = None

    @classmethod
    @abc.abstractmethod
    def info(cls, config: AppConfig) -> ProviderInfo: ...

    # -- state -------------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._disabled_reason is None

    @property
    def disabled_reason(self) -> str | None:
        return self._disabled_reason

    def disable(self, reason: str) -> None:
        if self._disabled_reason is None:
            self._disabled_reason = reason
            logger.warning("%s provider disabled: %s", self.display_name, reason)

    @property
    def limiter(self) -> AsyncRateLimiter | None:
        return None

    @property
    def request_stats(self) -> RequestStats:
        return RequestStats()

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        """Prepare the provider (e.g. check robots.txt). Must not raise for expected failures."""

    async def close(self) -> None:
        """Release network resources."""

    async def self_test(self) -> None:
        """Raise :class:`ProviderSelfTestError` if the endpoints misbehave."""

    async def __aenter__(self) -> Provider:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- checks ------------------------------------------------------------------------------

    @abc.abstractmethod
    async def check(self, username: str) -> CheckResult:
        """Check one name. Expected failures are returned as ERROR/UNKNOWN results, not raised."""

    async def check_many(self, usernames: Sequence[str]) -> list[CheckResult]:
        """Check several names; returns exactly one result per input name, in order."""
        return [await self.check(name) for name in usernames]

    def _result(self, username: str, status: Status, **fields: Any) -> CheckResult:
        return CheckResult(username=username, status=status, provider=self.name, **fields)

    def _error(self, username: str, message: str) -> CheckResult:
        return self._result(username, Status.ERROR, last_error=message)
