"""Rich renderables: startup banner, live scan dashboard, result panels and summaries."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import deque
from collections.abc import Iterable, Sequence
from datetime import datetime

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.panel import Panel
from rich.measure import Measurement
from rich.table import Table
from rich.text import Text

from . import APP_NAME, AUTHOR
from .availability import ReleaseKind, headline, release_for, short_label
from .http_client import RequestStats
from .models import CheckResult, Status, format_uuid
from .providers import provider_display_name
from .providers.base import Provider
from .ranking import score_breakdown
from .scanner import ScanOutcome, ScanState
from .timeutil import format_delta_short, format_duration, humanize_ago, utcnow

ACCENT = "cyan"
MUTED = "bright_black"
STATUS_STYLE: dict[Status, str] = {
    Status.AVAILABLE: "bold green",
    Status.SOON: "bold yellow",
    Status.TAKEN: "red",
    Status.BLOCKED: "magenta",
    Status.UNKNOWN: MUTED,
    Status.ERROR: "bold red",
}
BORDER_STYLE: dict[Status, str] = {
    Status.AVAILABLE: "green",
    Status.SOON: "yellow",
    Status.TAKEN: "red",
    Status.BLOCKED: "magenta",
    Status.UNKNOWN: MUTED,
    Status.ERROR: "red",
}
PHASE_STYLE = {"Scanning": f"bold {ACCENT}", "Stopping": "bold yellow", "Stopped": "bold yellow", "Done": "bold green"}

_FANCY_SYMBOLS: dict[Status, str] = {
    Status.AVAILABLE: "✓",
    Status.SOON: "◷",
    Status.TAKEN: "✗",
    Status.BLOCKED: "⊘",
    Status.UNKNOWN: "?",
    Status.ERROR: "!",
}
# The classic Windows console font (Consolas) has none of the symbols above, but has these.
_SIMPLE_SYMBOLS: dict[Status, str] = {
    Status.AVAILABLE: "√",
    Status.SOON: "→",
    Status.TAKEN: "×",
    Status.BLOCKED: "-",
    Status.UNKNOWN: "?",
    Status.ERROR: "!",
}


def simple_glyphs() -> bool:
    """True in the classic Windows console window, whose default font lacks ✓ ◷ ✗ and braille.

    Override with ``MCF_SYMBOLS=simple`` or ``MCF_SYMBOLS=fancy``.
    """
    choice = os.environ.get("MCF_SYMBOLS", "").strip().lower()
    if choice in ("simple", "ascii"):
        return True
    if choice in ("fancy", "unicode"):
        return False
    if sys.platform != "win32":
        return False
    # Windows Terminal, VS Code and mintty/Git Bash render (or fall back for) these glyphs.
    return not (os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM") or os.environ.get("TERM"))


def symbol(status: Status) -> str:
    return (_SIMPLE_SYMBOLS if simple_glyphs() else _FANCY_SYMBOLS)[status]


def mark(ok: bool = True) -> str:
    """Rich markup for a success/failure tick that the current console can display."""
    if ok:
        return f"[green]{symbol(Status.AVAILABLE)}[/]"
    return f"[red]{symbol(Status.TAKEN)}[/]"


def spinner_name() -> str:
    return "line" if simple_glyphs() else "dots"


def percent(done: int, total: int) -> str:
    """Floor-rounded percentage, so 319/320 shows 99% rather than a misleading 100%."""
    if total <= 0:
        return "0%"
    return f"{done * 100 // total}%"


def kv_grid(rows: Iterable[tuple[str, RenderableType]], *, min_label: int = 12) -> Table:
    grid = Table.grid(padding=(0, 3))
    grid.add_column(style=MUTED, no_wrap=True, min_width=min_label)
    grid.add_column(overflow="fold")
    for label, value in rows:
        grid.add_row(label, value)
    return grid


def banner(rows: Iterable[tuple[str, RenderableType]], *, title: str = APP_NAME) -> Panel:
    return Panel(
        kv_grid(rows, min_label=14),
        title=Text(f" {title} ", style=f"bold {ACCENT}"),
        subtitle=Text(f" made by {AUTHOR} ", style=MUTED) if title == APP_NAME else None,
        subtitle_align="right",
        border_style=ACCENT,
        box=box.ROUNDED,
        padding=(1, 3),
        expand=False,
    )


def symbol_text(status: Status) -> Text:
    return Text(symbol(status), style=STATUS_STYLE[status])


def discovery_line(result: CheckResult, now: datetime | None = None) -> Text:
    line = Text()
    line.append(f"{symbol(result.status)} ", style=STATUS_STYLE[result.status])
    line.append(f"{result.name:<16}", style="bold")
    line.append(f"  {short_label(result, now)}", style=MUTED)
    if result.quality_score is not None:
        line.append(f"  score {result.quality_score:.0f}", style=MUTED)
    return line


class BlockBar:
    """``█████░░░`` progress bar that stays readable without colour (logs, NO_COLOR, CI).

    Rich's own bar draws the unfilled part with the same glyph in a dimmer colour, which
    looks 100% full when colour is unavailable. Falls back to ``###---`` on non-UTF-8 consoles.
    """

    def __init__(self, total: float, completed: float, *, width: int | None = None, style: str = ACCENT) -> None:
        self.ratio = 0.0 if total <= 0 else max(0.0, min(1.0, completed / total))
        self.width = width
        self.style = style

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(self.width or 4, self.width or options.max_width)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = min(self.width or options.max_width, options.max_width)
        filled = int(round(self.ratio * width))
        full, empty = ("#", "-") if options.ascii_only else ("█", "░")
        yield Text.assemble((full * filled, self.style), (empty * (width - filled), MUTED), no_wrap=True, end="")


# -- live dashboard --------------------------------------------------------------------------


class RateMeter:
    """Sliding-window rate of a monotonically increasing counter."""

    def __init__(self, window: float = 30.0) -> None:
        self.window = window
        self._samples: deque[tuple[float, float]] = deque()

    def update(self, now: float, value: float) -> None:
        self._samples.append((now, value))
        while len(self._samples) > 2 and now - self._samples[0][0] > self.window:
            self._samples.popleft()

    def rate(self) -> float | None:
        if len(self._samples) < 2:
            return None
        (t0, v0), (t1, v1) = self._samples[0], self._samples[-1]
        if t1 - t0 < 5.0:  # too little data for a meaningful rate
            return None
        return (v1 - v0) / (t1 - t0)


class ScanDashboard:
    """Live view of a scan; rendered by ``rich.live.Live`` a few times per second."""

    def __init__(self, state: ScanState, provider: Provider, *, workers: int, max_width: int = 88) -> None:
        self.state = state
        self.provider = provider
        self.workers = workers
        self.max_width = max_width
        self._names = RateMeter()
        self._requests = RateMeter()

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield self.render(width=min(options.max_width, self.max_width))

    def render(self, width: int | None = None) -> Panel:
        now = time.monotonic()
        state = self.state
        stats = self.provider.request_stats
        self._names.update(now, state.session_processed)
        self._requests.update(now, stats.requests)
        names_rate = self._names.rate()
        request_rate = self._requests.rate()
        total = max(state.total, 1)
        processed = min(state.processed, total)
        remaining = max(state.total - state.processed, 0)
        eta = remaining / names_rate if names_rate else None

        header = Table.grid(expand=True, padding=(0, 1))
        header.add_column(no_wrap=True, min_width=10)
        header.add_column(ratio=1)
        header.add_column(justify="right", no_wrap=True, min_width=5)
        header.add_row(
            Text(state.phase, style=PHASE_STYLE.get(state.phase, "bold")),
            BlockBar(total, processed, style="green" if processed >= total else ACCENT),
            Text(percent(processed, total), style="bold"),
        )
        position = Text(f"{processed:,} / {state.total:,} names", style=MUTED, justify="right")

        counters = state.counters
        limiter = self.provider.limiter
        rate_text = Text("—" if request_rate is None else f"{request_rate:.2f} req/s")
        if limiter is not None:
            rate_text.append(f"  (limit {limiter.rate:.2f})", style=MUTED)
        requests_text = Text(f"{stats.requests:,}")
        if stats.rate_limited:
            requests_text.append(f"  {stats.rate_limited} × 429", style="yellow")
        available_text = Text(f"{counters.available:,}", style="bold green")
        if counters.confirmed:
            available_text.append(f" ({counters.confirmed:,} {symbol(Status.AVAILABLE)})", style="green")

        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=MUTED, no_wrap=True, min_width=10)
        grid.add_column(justify="right", no_wrap=True, min_width=9)
        grid.add_column(width=2)
        grid.add_column(style=MUTED, no_wrap=True, min_width=10)
        grid.add_column(no_wrap=True)
        rows = [
            ("Checked", Text(f"{counters.checked:,}"), "Rate", rate_text),
            ("Available", available_text, "Throughput", Text("—" if names_rate is None else f"{names_rate:.1f} names/s")),
            ("Soon", Text(f"{counters.soon:,}", style="bold yellow"), "ETA", Text(format_duration(eta))),
            ("Taken", Text(f"{counters.taken:,}", style="red"), "Cache hits", Text(f"{state.session.cache_hits:,}")),
            ("Other", Text(f"{counters.blocked + counters.unknown:,}", style=MUTED), "Requests", requests_text),
            ("Errors", Text(f"{counters.errors:,}", style="bold red" if counters.errors else ""), "Elapsed", Text(format_duration(now - state.started))),
        ]
        for left, left_value, right, right_value in rows:
            grid.add_row(left, left_value, "", right, right_value)

        discoveries = Table.grid(padding=(0, 2))
        discoveries.add_column(width=1)
        discoveries.add_column(min_width=16, style="bold", no_wrap=True)
        discoveries.add_column(style=MUTED, no_wrap=True)
        discoveries.add_column(justify="right", style=MUTED, no_wrap=True)
        wall_now = utcnow()
        for result in state.latest:
            score = "" if result.quality_score is None else f"score {result.quality_score:.0f}"
            discoveries.add_row(symbol_text(result.status), result.name, short_label(result, wall_now), score)
        if not state.latest:
            discoveries.add_row("", Text("none yet", style=MUTED), "", "")

        body = Group(
            header,
            position,
            Text(""),
            grid,
            Text(""),
            Text("Latest discoveries", style="bold"),
            discoveries,
        )
        return Panel(
            body,
            title=Text(f" {APP_NAME} ", style=f"bold {ACCENT}"),
            subtitle=self._footer(limiter),
            subtitle_align="left",
            border_style=ACCENT,
            box=box.ROUNDED,
            padding=(1, 2),
            width=width,
        )

    def _footer(self, limiter: object) -> Text:
        remaining = getattr(limiter, "cooldown_remaining", 0.0)
        if remaining > 0:
            return Text(f" {'||' if simple_glyphs() else '⏸'} rate limited (HTTP 429) — all workers paused for {format_duration(remaining)} ", style="yellow")
        if getattr(limiter, "is_throttled", False):
            return Text(f" rate reduced to {limiter.rate:.2f} req/s after a 429 — recovering slowly ", style="yellow")  # type: ignore[attr-defined]
        if self.state.phase == "Stopping":
            return Text(" stopping — saving progress… ", style="yellow")
        return Text(f" {self.workers} workers · Ctrl+C stops safely, progress is saved ", style=MUTED)


class PlainReporter:
    """Line-oriented output for non-interactive terminals (CI logs, pipes) or ``--plain``."""

    def __init__(self, console: Console, state: ScanState, *, interval: float = 15.0) -> None:
        self.console = console
        self.state = state
        self.interval = interval

    def on_result(self, result: CheckResult) -> None:
        if result.status.is_discovery and not result.from_cache:
            self.console.print(discovery_line(result))

    def progress_line(self) -> str:
        s = self.state
        c = s.counters
        pct = s.processed / max(s.total, 1)
        return (
            f"[{pct:4.0%}] {s.processed:,}/{s.total:,} checked · available {c.available:,} · "
            f"soon {c.soon:,} · taken {c.taken:,} · errors {c.errors:,} · cache hits {s.session.cache_hits:,}"
        )

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self.console.print(self.progress_line(), style=MUTED)


# -- single results --------------------------------------------------------------------------


def result_panel(result: CheckResult, *, now: datetime | None = None, width: int = 76) -> Panel:
    now = now or utcnow()
    rows: list[tuple[str, RenderableType]] = [
        ("Status", Text(headline(result, now), style=STATUS_STYLE[result.status])),
    ]
    estimate = release_for(result)
    if estimate.kind is ReleaseKind.AVAILABLE_CONFIRMED:
        rows.append(("Confidence", Text("confirmed by Mojang's availability check", style="green")))
    elif estimate.kind is ReleaseKind.AVAILABLE_UNVERIFIED:
        rows.append(("Confidence", Text("unverified — no profile found, claimability not checked", style="yellow")))
    elif estimate.kind is ReleaseKind.ESTIMATED and estimate.at is not None:
        rows.append(
            (
                "Release",
                Text(
                    f"ESTIMATED {estimate.at:%Y-%m-%d %H:%M} UTC (in ~{format_delta_short(estimate.at - now)}) — not exact",
                    style="yellow",
                ),
            )
        )
    elif estimate.kind is ReleaseKind.UNKNOWN:
        rows.append(("Release", Text("RELEASE UNKNOWN — no reliable timestamp", style="yellow")))
    if result.status is Status.TAKEN:
        profile = Text(result.name, style="bold")
        if result.uuid:
            profile.append(f"  {format_uuid(result.uuid)}", style=MUTED)
        rows.append(("Profile", profile))
    rows.append(("Provider", provider_display_name(result.provider)))
    if result.detail:
        rows.append(("Details", Text(result.detail, style=MUTED)))
    if result.last_error:
        rows.append(("Error", Text(result.last_error, style="red")))
    if result.quality_score is not None:
        rows.append(("Quality", f"{result.quality_score:.0f} / 100"))
    checked = humanize_ago(result.checked_at, now)
    rows.append(("Checked", f"{checked} (cached)" if result.from_cache else checked))
    return Panel(
        kv_grid(rows),
        title=Text(f" {result.name} ", style="bold"),
        title_align="left",
        border_style=BORDER_STYLE[result.status],
        box=box.ROUNDED,
        padding=(0, 1),
        width=width,
    )


def invalid_panel(name: str, message: str, *, width: int = 76) -> Panel:
    return Panel(
        kv_grid([("Status", Text("INVALID", style="bold red")), ("Reason", message), ("Network", Text("no request sent", style=MUTED))]),
        title=Text(f" {name or '(empty)'} ", style="bold"),
        title_align="left",
        border_style="red",
        box=box.ROUNDED,
        padding=(0, 1),
        width=width,
    )


def score_table(username: str, *, dictionary_word: bool) -> Table:
    breakdown = score_breakdown(username, dictionary_word=dictionary_word)
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column(style=MUTED)
    table.add_column(justify="right")
    for label, value in (
        ("Length", breakdown.length),
        ("Characters", breakdown.characters),
        ("Dictionary", breakdown.dictionary),
        ("Pronounceable", breakdown.pronounceability),
        ("Total", breakdown.total),
    ):
        table.add_row(label, f"{value:.1f}")
    return table


# -- summaries -------------------------------------------------------------------------------


def scan_summary(
    state: ScanState,
    outcome: ScanOutcome,
    stats: RequestStats,
    *,
    output_files: Sequence[str],
    top: int = 15,
) -> Group:
    session = state.session
    if outcome.completed:
        title, style = " Scan complete ", "green"
    elif outcome.interrupted:
        title, style = " Scan interrupted — progress saved ", "yellow"
    else:
        title, style = " Scan paused — some checks failed ", "yellow"

    available = Text(f"{session.available:,}", style="bold green")
    if session.confirmed:
        available.append(f"  ({session.confirmed:,} confirmed)", style="green")
    rows: list[tuple[str, RenderableType]] = [
        ("Checked this run", Text(f"{session.checked:,}  ", style="bold").append(f"({session.cache_hits:,} from cache)", style=MUTED)),
        ("Available", available),
        ("Soon", Text(f"{session.soon:,}", style="bold yellow")),
        ("Taken", f"{session.taken:,}"),
    ]
    if session.blocked or session.unknown:
        rows.append(("Blocked / unknown", f"{session.blocked:,} / {session.unknown:,}"))
    errors = Text(f"{session.errors:,}", style="bold red" if session.errors else "")
    if session.errors:
        errors.append("  — retried automatically on the next run", style=MUTED)
    rows.append(("Errors", errors))
    requests = Text(f"{stats.requests:,}")
    requests.append(f"  ({stats.retries:,} retries, {stats.rate_limited:,} × HTTP 429)", style=MUTED)
    rows.append(("Requests", requests))
    rows.append(("Elapsed", format_duration(outcome.elapsed)))
    rows.append(("Job progress", f"{outcome.done:,} / {outcome.total:,} ({percent(outcome.done, outcome.total)})"))

    parts: list[RenderableType] = [
        Panel(kv_grid(rows, min_label=17), title=Text(title, style=f"bold {style}"), border_style=style, box=box.ROUNDED, padding=(1, 2), expand=False)
    ]
    fresh = [r for r in state.discoveries if not r.from_cache]
    if fresh:
        best = sorted(fresh, key=lambda r: (-(r.quality_score or 0), r.name))[:top]
        table = Table(title="Discoveries this run (best first)", title_justify="left", box=box.SIMPLE_HEAD, header_style=MUTED, expand=False)
        table.add_column("", width=1)
        table.add_column("Username", style="bold")
        table.add_column("Status")
        table.add_column("Score", justify="right")
        now = utcnow()
        for result in best:
            table.add_row(symbol_text(result.status), result.name, short_label(result, now), f"{result.quality_score or 0:.0f}")
        if len(fresh) > top:
            table.caption = f"… and {len(fresh) - top:,} more — see the output files"
        parts.append(table)
    if output_files:
        parts.append(Text.assemble(("Output  ", MUTED), " · ".join(output_files)))
    if outcome.interrupted or not outcome.completed:
        parts.append(Text("Run the same command again to resume where this run stopped.", style="yellow"))
    return Group(*parts)


def status_overview(counts: dict[Status, int], confirmed: int) -> Table:
    total = sum(counts.values())
    table = Table(box=box.SIMPLE_HEAD, header_style=MUTED, expand=False)
    table.add_column("", width=1)
    table.add_column("Status")
    table.add_column("Names", justify="right")
    table.add_column("Share", justify="right")
    table.add_column("", min_width=20)
    for status in Status:
        count = counts.get(status, 0)
        share = count / total if total else 0.0
        label = status.value
        if status is Status.AVAILABLE and confirmed:
            label += f" ({confirmed:,} confirmed)"
        bar = BlockBar(1.0, share, width=20, style=BORDER_STYLE[status])
        table.add_row(symbol_text(status), Text(label, style=STATUS_STYLE[status]), f"{count:,}", f"{share:.1%}", bar)
    table.add_row("", Text("Total", style="bold"), Text(f"{total:,}", style="bold"), "", "")
    return table


def results_preview(results: Sequence[CheckResult], *, title: str) -> Table:
    table = Table(title=title, title_justify="left", box=box.SIMPLE_HEAD, header_style=MUTED, expand=False)
    table.add_column("", width=1)
    table.add_column("Username", style="bold")
    table.add_column("Status")
    table.add_column("Score", justify="right")
    table.add_column("Checked", style=MUTED)
    now = utcnow()
    for result in results:
        label = short_label(result, now) if result.status.is_discovery else result.status.value.lower()
        table.add_row(
            symbol_text(result.status),
            result.name,
            Text(label, style=STATUS_STYLE[result.status]),
            "" if result.quality_score is None else f"{result.quality_score:.0f}",
            humanize_ago(result.checked_at, now),
        )
    return table

