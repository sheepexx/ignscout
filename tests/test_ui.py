"""The Rich renderables must render without errors and show the key information."""

import io
from datetime import timedelta

from rich.console import Console

from minecraft_finder.config import AppConfig
from minecraft_finder.models import CheckResult, Confidence, Status
from minecraft_finder.providers.demo import DemoProvider
from minecraft_finder.scanner import ScanCounters, ScanOutcome, ScanState
from minecraft_finder.timeutil import utcnow
from minecraft_finder.ui import ScanDashboard, result_panel, scan_summary, status_overview


def render(renderable, width=100) -> str:
    console = Console(width=width, record=True, force_terminal=False, color_system=None)
    console.print(renderable)
    return console.export_text()


def sample_state() -> ScanState:
    state = ScanState(total=1000, done_at_start=500, counters=ScanCounters(checked=630, available=12, soon=2, taken=610, errors=6))
    state.session.checked = 130
    state.latest.appendleft(CheckResult("glacier", Status.AVAILABLE, "minecraft", confidence=Confidence.UNVERIFIED, quality_score=88))
    state.latest.appendleft(
        CheckResult("daylight", Status.SOON, "minecraft+namemc", confidence=Confidence.ESTIMATED, available_at=utcnow() + timedelta(days=3, hours=1), quality_score=74)
    )
    state.phase = "Scanning"
    return state


def test_dashboard_renders_progress_counters_and_discoveries():
    provider = DemoProvider(AppConfig().providers.demo, AppConfig().scanner)
    text = render(ScanDashboard(sample_state(), provider, workers=4))
    for expected in ("Minecraft Name Finder", "Scanning", "63%", "630 / 1,000", "Available", "Latest discoveries", "glacier", "likely available", "daylight", "estimated 3d"):
        assert expected in text, expected


def test_dashboard_fits_narrow_terminals():
    provider = DemoProvider(AppConfig().providers.demo, AppConfig().scanner)
    for line in render(ScanDashboard(sample_state(), provider, workers=4), width=60).splitlines():
        assert len(line) <= 60


def test_result_panels_for_every_status():
    now = utcnow()
    cases = [
        (CheckResult("glacier", Status.AVAILABLE, "minecraft", confidence=Confidence.CONFIRMED), "AVAILABLE NOW"),
        (CheckResult("glacier", Status.AVAILABLE, "minecraft", confidence=Confidence.UNVERIFIED), "LIKELY AVAILABLE"),
        (CheckResult("dusk", Status.SOON, "minecraft"), "RELEASE UNKNOWN"),
        (CheckResult("dusk", Status.SOON, "minecraft", confidence=Confidence.ESTIMATED, available_at=now + timedelta(days=2)), "ESTIMATED RELEASE"),
        (CheckResult("notch", Status.TAKEN, "minecraft", display_name="Notch", uuid="069a79f444e94726a5befca90e38aaf5"), "069a79f4-44e9-4726-a5be-fca90e38aaf5"),
        (CheckResult("badword", Status.BLOCKED, "minecraft"), "NOT ALLOWED"),
        (CheckResult("x_y_z", Status.ERROR, "minecraft", last_error="timeout"), "timeout"),
    ]
    for result, expected in cases:
        text = render(result_panel(result, now=now))
        assert expected in text
        assert "Checked" in text


def test_summary_and_overview_render():
    state = sample_state()
    state.discoveries.extend(state.latest)
    outcome = ScanOutcome(completed=False, interrupted=True, total=1000, done=630, session_processed=130, session_errors=0, elapsed=75.0)
    from minecraft_finder.http_client import RequestStats

    text = render(scan_summary(state, outcome, RequestStats(requests=13, rate_limited=1), output_files=["output/available.txt"]))
    assert "interrupted" in text
    assert "resume" in text
    assert "output/available.txt" in text
    overview = render(status_overview({Status.AVAILABLE: 3, Status.TAKEN: 7}, confirmed=1))
    assert "1 confirmed" in overview and "Total" in overview


def test_block_bar_is_readable_without_colour():
    from minecraft_finder.ui import BlockBar

    assert render(BlockBar(100, 25, width=20), width=40).strip() == "█████░░░░░░░░░░░░░░░"
    ascii_stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    ascii_console = Console(file=ascii_stream, width=40, color_system=None, force_terminal=False)
    ascii_console.print(BlockBar(100, 50, width=10))
    ascii_stream.flush()
    assert ascii_stream.buffer.getvalue().decode("ascii").strip() == "#####-----"


def test_footer_shows_rate_limit_cooldown():
    provider = DemoProvider(AppConfig().providers.demo, AppConfig().scanner)
    provider.limiter.penalize(45.0)
    text = render(ScanDashboard(sample_state(), provider, workers=4))
    assert "rate limited (HTTP 429)" in text
    assert "paused" in text


def test_percent_never_rounds_up_to_done():
    from minecraft_finder.ui import percent

    assert percent(319, 320) == "99%"
    assert percent(320, 320) == "100%"
    assert percent(0, 0) == "0%"
