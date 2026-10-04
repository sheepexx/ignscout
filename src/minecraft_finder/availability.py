"""Modelling *when* a Minecraft Java name becomes claimable.

Facts this module relies on (Minecraft Help Center, "View or Change Your In-Game
Profile Name in Minecraft", help.minecraft.net article 4408950195341):

* a Java Edition profile name can be changed once every 30 days;
* after a change the previous name stays reserved for 37 days: for the first
  30 days nobody can take it, during days 30-37 only the previous owner can
  take it back, and after day 37 anyone may claim it.

Mojang removed the public name-history endpoint on 2022-09-13, so the official
API cannot say *when* a name was dropped. Therefore:

* an exact release time is never claimed — at best it is an ESTIMATE;
* estimates are only produced from an explicit name-change timestamp, or from a
  third-party release time that is consistent with the 37-day rule;
* everything else is reported as RELEASE UNKNOWN.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import CheckResult, Confidence, Status
from .timeutil import format_delta_short, utcnow

NAME_CHANGE_COOLDOWN = timedelta(days=30)
NAME_HOLD_PERIOD = timedelta(days=37)
OWNER_RECLAIM_WINDOW = NAME_HOLD_PERIOD - NAME_CHANGE_COOLDOWN
#: Slack for clock differences and providers rounding their timestamps.
ESTIMATE_TOLERANCE = timedelta(days=1)


class ReleaseKind(enum.StrEnum):
    AVAILABLE_CONFIRMED = "available_confirmed"  # claimable now, confirmed by Mojang
    AVAILABLE_UNVERIFIED = "available_unverified"  # no profile uses it; claimability unchecked
    ESTIMATED = "estimated"  # held; estimated release time known
    UNKNOWN = "unknown"  # held or unclear; release time cannot be determined
    NOT_RELEASING = "not_releasing"  # taken / blocked / unknown status


@dataclass(frozen=True, slots=True)
class ReleaseEstimate:
    kind: ReleaseKind
    at: datetime | None = None
    basis: str = ""


def estimate_from_name_change(changed_at: datetime | None, *, now: datetime | None = None) -> ReleaseEstimate:
    """Estimate when a name dropped at ``changed_at`` becomes claimable by anyone.

    Even with an exact timestamp this is only an estimate: Mojang may change the
    rules, the previous owner may reclaim the name during days 30-37, and the
    name could be blocked.
    """
    now = now or utcnow()
    if changed_at is None:
        return ReleaseEstimate(ReleaseKind.UNKNOWN, None, "no name-change timestamp available")
    if changed_at > now + timedelta(minutes=5):
        return ReleaseEstimate(
            ReleaseKind.UNKNOWN, None, "name-change timestamp lies in the future (inconsistent data)"
        )
    release = changed_at + NAME_HOLD_PERIOD
    if release <= now:
        return ReleaseEstimate(
            ReleaseKind.UNKNOWN,
            None,
            "the 37-day hold has already elapsed, yet the name is not claimable",
        )
    return ReleaseEstimate(
        ReleaseKind.ESTIMATED,
        release,
        f"name changed {changed_at:%Y-%m-%d %H:%M} UTC + 37-day hold",
    )


def validate_reported_release(
    at: datetime | None, *, source: str, now: datetime | None = None
) -> ReleaseEstimate:
    """Sanity-check a release time reported by a third party.

    Only times inside the 37-day hold window (from now) are plausible; anything
    else is treated as unreliable and downgraded to RELEASE UNKNOWN.
    """
    now = now or utcnow()
    if at is None:
        return ReleaseEstimate(ReleaseKind.UNKNOWN, None, f"{source} reported no release time")
    if at <= now:
        return ReleaseEstimate(
            ReleaseKind.UNKNOWN, None, f"{source}'s release time has already passed (re-check later)"
        )
    if at > now + NAME_HOLD_PERIOD + ESTIMATE_TOLERANCE:
        return ReleaseEstimate(
            ReleaseKind.UNKNOWN,
            None,
            f"{source}'s release time is further away than the 37-day hold allows (not trusted)",
        )
    return ReleaseEstimate(ReleaseKind.ESTIMATED, at, f"reported by {source}")


def release_for(result: CheckResult) -> ReleaseEstimate:
    if result.status is Status.AVAILABLE:
        if result.confidence is Confidence.CONFIRMED:
            return ReleaseEstimate(ReleaseKind.AVAILABLE_CONFIRMED)
        return ReleaseEstimate(ReleaseKind.AVAILABLE_UNVERIFIED)
    if result.status is Status.SOON:
        if result.available_at is not None:
            return ReleaseEstimate(ReleaseKind.ESTIMATED, result.available_at, result.detail or "")
        return ReleaseEstimate(ReleaseKind.UNKNOWN, None, result.detail or "")
    return ReleaseEstimate(ReleaseKind.NOT_RELEASING)


_STATUS_HEADLINES = {
    Status.TAKEN: "TAKEN",
    Status.BLOCKED: "NOT ALLOWED",
    Status.UNKNOWN: "UNKNOWN",
    Status.ERROR: "ERROR",
}


def headline(result: CheckResult, now: datetime | None = None) -> str:
    """Big status line, e.g. ``AVAILABLE NOW`` or ``ESTIMATED RELEASE 2026-10-05``."""
    now = now or utcnow()
    estimate = release_for(result)
    match estimate.kind:
        case ReleaseKind.AVAILABLE_CONFIRMED:
            return "AVAILABLE NOW"
        case ReleaseKind.AVAILABLE_UNVERIFIED:
            return "LIKELY AVAILABLE"
        case ReleaseKind.ESTIMATED:
            assert estimate.at is not None
            if estimate.at <= now:
                return "ESTIMATED RELEASE PASSED — RE-CHECK"
            return f"ESTIMATED RELEASE {estimate.at:%Y-%m-%d}"
        case ReleaseKind.UNKNOWN:
            return "RELEASE UNKNOWN"
    if result.status is Status.BLOCKED and result.confidence is Confidence.UNVERIFIED:  # local word filter
        return "PROBABLY NOT ALLOWED"
    return _STATUS_HEADLINES[result.status]


def short_label(result: CheckResult, now: datetime | None = None) -> str:
    """Compact label for dashboard lines, e.g. ``estimated 3d · 2026-10-05``."""
    now = now or utcnow()
    estimate = release_for(result)
    match estimate.kind:
        case ReleaseKind.AVAILABLE_CONFIRMED:
            return "available now (confirmed)"
        case ReleaseKind.AVAILABLE_UNVERIFIED:
            return "likely available"
        case ReleaseKind.ESTIMATED:
            assert estimate.at is not None
            return f"estimated {format_delta_short(estimate.at - now)} · {estimate.at:%Y-%m-%d}"
        case ReleaseKind.UNKNOWN:
            return "release unknown"
    if result.status is Status.BLOCKED:
        return "probably not allowed" if result.confidence is Confidence.UNVERIFIED else "not allowed"
    return result.status.value.lower()


def combine_with_enrichment(primary: CheckResult, secondary: CheckResult) -> CheckResult:
    """Merge an enrichment provider's opinion into an *unverified* AVAILABLE result.

    Only conclusions the secondary data can justify are applied:

    * secondary SOON      -> SOON (its release time was already sanity-checked by the provider)
    * secondary TAKEN or BLOCKED -> UNKNOWN (sources disagree; never guess)
    * anything else       -> unchanged (a non-authoritative "available" adds no certainty)
    """
    if primary.status is not Status.AVAILABLE or primary.confidence is Confidence.CONFIRMED:
        return primary
    provider = f"{primary.provider}+{secondary.provider}"
    if secondary.status is Status.SOON:
        if secondary.available_at is not None:
            return primary.evolve(
                status=Status.SOON,
                confidence=Confidence.ESTIMATED,
                available_at=secondary.available_at,
                provider=provider,
                detail=(
                    f"No Minecraft profile uses this name; {secondary.provider} reports it is held "
                    f"until about {secondary.available_at:%Y-%m-%d %H:%M} UTC. Estimate only."
                ),
            )
        return primary.evolve(
            status=Status.SOON,
            confidence=Confidence.NONE,
            available_at=None,
            provider=provider,
            detail=(
                f"No Minecraft profile uses this name; {secondary.provider} reports it as held. "
                f"Release time unknown ({secondary.detail or 'no reliable timestamp'})."
            ),
        )
    if secondary.status in (Status.TAKEN, Status.BLOCKED):
        return primary.evolve(
            status=Status.UNKNOWN,
            confidence=Confidence.NONE,
            provider=provider,
            detail=(
                f"Sources disagree: Mojang has no profile with this name, but {secondary.provider} "
                "lists it as unavailable."
            ),
        )
    return primary
