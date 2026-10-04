"""Small time helpers. Every timestamp in the application is timezone-aware UTC."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([wdhms])")
_UNIT_SECONDS = {"w": 604_800, "d": 86_400, "h": 3_600, "m": 60, "s": 1}


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_iso(dt: datetime | None) -> str | None:
    """Fixed-width ISO-8601 UTC string, so stored values sort lexicographically by time."""
    if dt is None:
        return None
    return ensure_utc(dt).strftime(_ISO_FORMAT)


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return ensure_utc(datetime.fromisoformat(value))


def parse_duration(text: str) -> timedelta:
    """Parse durations such as ``24h``, ``30m``, ``7d``, ``1w``, ``1h30m`` or ``90s``.

    A bare number is interpreted as hours (``--cache-ttl 12`` means 12 hours).
    """
    s = text.strip().lower()
    if not s:
        raise ValueError("empty duration")
    try:
        hours = float(s)
    except ValueError:
        pass
    else:
        if hours < 0 or not math.isfinite(hours):
            raise ValueError(f"invalid duration: {text!r}")
        return timedelta(hours=hours)

    total = 0.0
    pos = 0
    for match in _DURATION_PART.finditer(s):
        if s[pos : match.start()].strip():
            break
        total += float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
        pos = match.end()
    if pos == 0 or s[pos:].strip():
        raise ValueError(f"invalid duration: {text!r} (examples: 24h, 30m, 7d, 1h30m)")
    return timedelta(seconds=total)


def format_duration(seconds: float | None) -> str:
    """Compact duration for dashboards: ``1h 42m``, ``3m 05s``, ``12s``, ``2d 4h``."""
    if seconds is None or not math.isfinite(seconds):
        return "—"
    total = max(0, int(round(seconds)))
    days, rem = divmod(total, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_ttl(delta: timedelta) -> str:
    """Readable cache TTL: ``24h``, ``7d``, ``30m``, ``1h 30m``."""
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "0 (cache disabled)"
    if seconds % 86_400 == 0 and seconds >= 3 * 86_400:
        return f"{seconds // 86_400}d"
    if seconds % 3_600 == 0:
        return f"{seconds // 3_600}h"
    return format_duration(seconds)


def format_delta_short(delta: timedelta) -> str:
    """Single-unit approximation used next to dates: ``3d``, ``5h``, ``12m``, ``<1m``."""
    seconds = max(0.0, delta.total_seconds())
    if seconds >= 86_400:
        return f"{int(seconds // 86_400)}d"
    if seconds >= 3_600:
        return f"{int(seconds // 3_600)}h"
    if seconds >= 60:
        return f"{int(seconds // 60)}m"
    return "<1m"


def humanize_ago(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "never"
    seconds = ((now or utcnow()) - dt).total_seconds()
    if seconds < 10:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3_600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86_400:
        return f"{int(seconds // 3_600)}h ago"
    return f"{int(seconds // 86_400)}d ago"
