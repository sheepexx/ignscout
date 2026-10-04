"""Core data types shared by providers, the scanner, the database and the UI."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from .timeutil import to_iso, utcnow


class Status(enum.StrEnum):
    """Outcome of a username check."""

    AVAILABLE = "AVAILABLE"  # no profile uses the name — see Confidence for how sure we are
    SOON = "SOON"  # not claimable yet, but expected to be released
    TAKEN = "TAKEN"  # an active profile uses the name
    BLOCKED = "BLOCKED"  # Mojang refuses the name (NOT_ALLOWED)
    UNKNOWN = "UNKNOWN"  # the available data does not allow a reliable answer
    ERROR = "ERROR"  # the check itself failed (network, rate limit, unexpected response)

    @property
    def is_definitive(self) -> bool:
        """Definitive results may be served from the cache; UNKNOWN and ERROR are re-checked."""
        return self in _DEFINITIVE

    @property
    def is_discovery(self) -> bool:
        return self is Status.AVAILABLE or self is Status.SOON


_DEFINITIVE = frozenset({Status.AVAILABLE, Status.SOON, Status.TAKEN, Status.BLOCKED})


class Confidence(enum.StrEnum):
    """How a result was established."""

    CONFIRMED = "confirmed"  # an authoritative endpoint said so
    UNVERIFIED = "unverified"  # inferred, e.g. "no profile found" without a claimability check
    ESTIMATED = "estimated"  # a time-based estimate (SOON with available_at)
    NONE = "none"


@dataclass(slots=True)
class CheckResult:
    username: str
    status: Status
    provider: str
    checked_at: datetime = field(default_factory=utcnow)
    confidence: Confidence = Confidence.NONE
    uuid: str | None = None
    display_name: str | None = None
    available_at: datetime | None = None
    detail: str | None = None
    last_error: str | None = None
    quality_score: float | None = None
    source_word: str | None = None
    from_cache: bool = False

    @property
    def key(self) -> str:
        """Usernames are case-insensitive; this is the canonical storage key."""
        return self.username.lower()

    @property
    def name(self) -> str:
        return self.display_name or self.username

    def evolve(self, **changes: Any) -> CheckResult:
        return replace(self, **changes)

    def to_json(self) -> dict[str, Any]:
        return {
            "username": self.name,
            "status": self.status.value.lower(),
            "confidence": self.confidence.value,
            "checked_at": to_iso(self.checked_at),
            "provider": self.provider,
            "uuid": format_uuid(self.uuid),
            "available_at": to_iso(self.available_at),
            "detail": self.detail,
            "error": self.last_error,
            "quality_score": self.quality_score,
            "source_word": self.source_word,
        }


def format_uuid(value: str | None) -> str | None:
    """Render Mojang's undashed UUIDs in the usual 8-4-4-4-12 form."""
    if not value:
        return value
    compact = value.replace("-", "").lower()
    if len(compact) != 32:
        return value
    return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"
