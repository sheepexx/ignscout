from datetime import UTC, datetime, timedelta

from minecraft_finder.availability import (
    NAME_CHANGE_COOLDOWN,
    NAME_HOLD_PERIOD,
    OWNER_RECLAIM_WINDOW,
    ReleaseKind,
    combine_with_enrichment,
    estimate_from_name_change,
    headline,
    short_label,
    validate_reported_release,
)
from minecraft_finder.models import CheckResult, Confidence, Status

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def result(status, **kw):
    return CheckResult(username="daylight", status=status, provider="minecraft", checked_at=NOW, **kw)


def test_hold_model_matches_mojang_rules():
    assert NAME_CHANGE_COOLDOWN == timedelta(days=30)
    assert NAME_HOLD_PERIOD == timedelta(days=37)
    assert OWNER_RECLAIM_WINDOW == timedelta(days=7)


def test_estimate_from_name_change():
    changed = NOW - timedelta(days=10)
    estimate = estimate_from_name_change(changed, now=NOW)
    assert estimate.kind is ReleaseKind.ESTIMATED
    assert estimate.at == changed + timedelta(days=37)


def test_no_timestamp_means_unknown():
    assert estimate_from_name_change(None, now=NOW).kind is ReleaseKind.UNKNOWN


def test_future_timestamp_is_inconsistent():
    assert estimate_from_name_change(NOW + timedelta(days=1), now=NOW).kind is ReleaseKind.UNKNOWN


def test_elapsed_hold_does_not_produce_a_date():
    estimate = estimate_from_name_change(NOW - timedelta(days=40), now=NOW)
    assert estimate.kind is ReleaseKind.UNKNOWN
    assert estimate.at is None


def test_reported_release_must_fit_the_hold_window():
    assert validate_reported_release(NOW + timedelta(days=3), source="X", now=NOW).kind is ReleaseKind.ESTIMATED
    assert validate_reported_release(NOW + timedelta(days=37, hours=12), source="X", now=NOW).kind is ReleaseKind.ESTIMATED
    assert validate_reported_release(NOW - timedelta(hours=1), source="X", now=NOW).kind is ReleaseKind.UNKNOWN
    assert validate_reported_release(NOW + timedelta(days=60), source="X", now=NOW).kind is ReleaseKind.UNKNOWN
    assert validate_reported_release(None, source="X", now=NOW).kind is ReleaseKind.UNKNOWN


def test_headlines_distinguish_confirmed_from_estimated():
    assert headline(result(Status.AVAILABLE, confidence=Confidence.CONFIRMED), NOW) == "AVAILABLE NOW"
    assert headline(result(Status.AVAILABLE, confidence=Confidence.UNVERIFIED), NOW) == "LIKELY AVAILABLE"
    soon = result(Status.SOON, confidence=Confidence.ESTIMATED, available_at=NOW + timedelta(days=3))
    assert headline(soon, NOW) == "ESTIMATED RELEASE 2026-10-05"
    assert headline(result(Status.SOON), NOW) == "RELEASE UNKNOWN"
    passed = result(Status.SOON, confidence=Confidence.ESTIMATED, available_at=NOW - timedelta(hours=1))
    assert "PASSED" in headline(passed, NOW)
    assert headline(result(Status.TAKEN), NOW) == "TAKEN"
    assert headline(result(Status.BLOCKED), NOW) == "NOT ALLOWED"


def test_short_labels():
    soon = result(Status.SOON, confidence=Confidence.ESTIMATED, available_at=NOW + timedelta(days=3, hours=2))
    assert short_label(soon, NOW) == "estimated 3d · 2026-10-05"
    assert short_label(result(Status.SOON), NOW) == "release unknown"
    assert short_label(result(Status.AVAILABLE, confidence=Confidence.UNVERIFIED), NOW) == "likely available"


def test_combine_with_enrichment():
    primary = result(Status.AVAILABLE, confidence=Confidence.UNVERIFIED)
    at = NOW + timedelta(days=2)
    soon = CheckResult("daylight", Status.SOON, "namemc", confidence=Confidence.ESTIMATED, available_at=at)
    merged = combine_with_enrichment(primary, soon)
    assert (merged.status, merged.confidence, merged.available_at) == (Status.SOON, Confidence.ESTIMATED, at)
    assert merged.provider == "minecraft+namemc"

    no_time = combine_with_enrichment(primary, CheckResult("daylight", Status.SOON, "namemc"))
    assert no_time.status is Status.SOON and no_time.available_at is None

    conflict = combine_with_enrichment(primary, CheckResult("daylight", Status.TAKEN, "namemc"))
    assert conflict.status is Status.UNKNOWN

    agree = combine_with_enrichment(primary, CheckResult("daylight", Status.AVAILABLE, "namemc"))
    assert agree is primary

    confirmed = result(Status.AVAILABLE, confidence=Confidence.CONFIRMED)
    assert combine_with_enrichment(confirmed, soon) is confirmed
