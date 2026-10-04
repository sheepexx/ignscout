"""Command-line interface (Typer + Rich)."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import csv
import json
import logging
import os
import re
import signal
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import IO, Annotated, NoReturn
from urllib.parse import urlsplit

import typer
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

from . import APP_NAME, AUTHOR, __version__, keepawake
from .availability import combine_with_enrichment
from .config import AppConfig, ConfigError, load_config
from .database import Database, JobRecord
from .http_client import RequestStats
from .logging_setup import setup_logging
from .models import CheckResult, Confidence, Status
from .namefilter import flag_if_offensive, reclassify_existing
from .output import ResultWriter
from .providers import (
    PROVIDERS,
    SCAN_PROVIDERS,
    Provider,
    ProviderSelfTestError,
    build_provider,
    provider_display_name,
)
from .providers.minecraft import BULK_LIMIT, DOCUMENTED_AVAILABILITY_LIMIT_RPS, DOCUMENTED_IP_LIMIT_RPS, MinecraftProvider
from .ranking import quality_score
from .scanner import ScanCounters, ScanOptions, ScanOutcome, Scanner, ScanState
from .scanlock import ScanLock, lock_path_for
from .timeutil import format_duration, format_ttl, humanize_ago, parse_duration, utcnow
from .ui import (
    ACCENT,
    MUTED,
    PlainReporter,
    ScanDashboard,
    banner,
    discovery_line,
    invalid_panel,
    mark,
    percent,
    result_panel,
    results_preview,
    scan_summary,
    spinner_name,
    status_overview,
)
from .validator import validate_username
from .wordlist import (
    CandidateFilter,
    CandidateOptions,
    ReadStats,
    Transform,
    is_dictionary_word,
    iter_candidates,
    wordlist_fingerprint,
)
from .wordsource import SHORT_NAME_LENGTHS, ShortNameKind, short_name_count, short_name_wordlist

logger = logging.getLogger(__name__)

EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

app = typer.Typer(
    name="minecraft-finder",
    help=f"[bold]{APP_NAME}[/] — find available and soon-to-be-available Minecraft Java usernames. "
    "Run it without a command for a simple menu.",
    add_completion=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_enable=False,
)
database_app = typer.Typer(help="Inspect and maintain the results database.", no_args_is_help=True)
app.add_typer(database_app, name="database")


class ScanProvider(StrEnum):
    minecraft = "minecraft"
    demo = "demo"


class CheckProvider(StrEnum):
    minecraft = "minecraft"
    namemc = "namemc"
    demo = "demo"


class SortKey(StrEnum):
    best = "best"
    quality = "quality"
    name = "name"
    length = "length"
    checked = "checked"
    release = "release"


class ExportFormat(StrEnum):
    csv = "csv"
    json = "json"
    jsonl = "jsonl"
    txt = "txt"


class StatusChoice(StrEnum):
    available = "available"
    soon = "soon"
    taken = "taken"
    blocked = "blocked"
    unknown = "unknown"
    error = "error"
    all = "all"


@dataclass
class AppContext:
    config: AppConfig
    console: Console
    verbose: bool
    debug: bool
    log_file: Path


DbOption = Annotated[
    Path | None,
    typer.Option("--db", help="SQLite database path (default: from config, data/results.db).", dir_okay=False),
]
OutputDirOption = Annotated[
    Path | None,
    typer.Option("--output-dir", help="Output directory (default: from config, output/).", file_okay=False),
]
DemoOption = Annotated[bool, typer.Option("--demo", help="Use the demo provider's separate database.")]


# -- helpers ---------------------------------------------------------------------------------


def _app(ctx: typer.Context) -> AppContext:
    return ctx.obj  # type: ignore[no-any-return]


def _fail(console: Console, message: str, code: int = EXIT_USAGE) -> NoReturn:
    console.print(f"[bold red]Error:[/] {message}")
    raise typer.Exit(code)


def _tidy(cfg: AppConfig, database: Database) -> None:
    """Reclassify stored "available" names that Mojang's word filter would refuse."""
    if cfg.scanner.filter_offensive:
        changed = reclassify_existing(database)
        if changed:
            logger.info("marked %d offensive names as probably not allowed", changed)


def _read_token(cfg: AppConfig) -> str | None:
    value = os.environ.get(cfg.providers.minecraft.token_env, "").strip()
    return value or None


def _paths(cfg: AppConfig, provider_name: str, db: Path | None, output_dir: Path | None) -> tuple[Path, Path]:
    """Database and output locations; the demo provider never mixes with real results."""
    db_path = db or Path(cfg.database.path)
    out_dir = output_dir or Path(cfg.output.directory)
    if provider_name == "demo":
        if db is None:
            db_path = db_path.with_name("demo-results.db")
        if output_dir is None:
            out_dir = out_dir / "demo"
    return db_path, out_dir


def _db_path(cfg: AppConfig, db: Path | None, demo: bool) -> Path:
    return _paths(cfg, "demo" if demo else "minecraft", db, None)[0]


def _with_overrides(
    cfg: AppConfig,
    *,
    workers: int | None = None,
    requests_per_second: float | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
    cache_ttl: str | None = None,
) -> AppConfig:
    merged = copy.deepcopy(cfg)
    if workers is not None:
        merged.scanner.workers = workers
    if requests_per_second is not None:
        merged.scanner.requests_per_second = requests_per_second
    if timeout is not None:
        merged.scanner.timeout = timeout
    if max_retries is not None:
        merged.scanner.max_retries = max_retries
    if cache_ttl is not None:
        merged.cache.ttl_hours = parse_duration(cache_ttl).total_seconds() / 3600
    merged.validate()
    return merged


def _install_break_handler(task: asyncio.Task[object] | None) -> Callable[[], None]:
    """On Windows, make Ctrl+Break stop the scan as gracefully as Ctrl+C does."""
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is None or task is None:
        return lambda: None
    loop = asyncio.get_running_loop()

    def handler(signum: int, frame: object) -> None:
        loop.call_soon_threadsafe(task.cancel)

    try:
        previous = signal.signal(sigbreak, handler)
    except ValueError:  # not in the main thread
        return lambda: None
    return lambda: signal.signal(sigbreak, previous if previous is not None else signal.SIG_DFL)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"minecraft-finder {__version__}, made by {AUTHOR}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to config.toml (default: ./config.toml if present).", dir_okay=False),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show informational log messages.")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="Show debug log messages and tracebacks.")] = False,
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True, help="Show the version and exit.")
    ] = False,
) -> None:
    console = Console(highlight=False)
    try:
        cfg, warnings = load_config(config)
    except ConfigError as exc:
        console.print(f"[bold red]Config error:[/] {exc}")
        raise typer.Exit(EXIT_USAGE) from None
    log_file = setup_logging(Path(cfg.logging.directory), verbose=verbose, debug=debug, console=console)
    for warning in warnings:
        console.print(f"[yellow]config:[/] {warning}")
    logger.info("minecraft-finder %s: %s (config: %s)", __version__, ctx.invoked_subcommand, cfg.source or "defaults")
    ctx.obj = AppContext(config=cfg, console=console, verbose=verbose, debug=debug, log_file=log_file)
    if ctx.invoked_subcommand is None:
        from .menu import run_menu  # imported lazily: the menu itself builds on these commands

        run_menu(ctx)


@app.command("menu")
def menu_command(ctx: typer.Context) -> None:
    """Open the simple numbered menu (same as running without a command)."""
    from .menu import run_menu

    run_menu(ctx)


# -- scan ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanPlan:
    provider_name: str
    verify: bool
    token: str | None
    namemc: bool
    output_dir: Path
    live: bool
    self_test: bool
    force: bool
    keep_awake: bool = True


def _stage(console: Console, database: Database, job: JobRecord, wordlist: Path, options: CandidateOptions) -> JobRecord:
    size = max(wordlist.stat().st_size, 1)
    stats = ReadStats()
    progress = Progress(
        SpinnerColumn(spinner_name(), style=ACCENT),
        TextColumn("[bold]Reading word list"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("[bright_black]{task.fields[lines]:,} lines · {task.fields[accepted]:,} candidates"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )
    with progress:
        task = progress.add_task("read", total=size, lines=0, accepted=0)

        def report(current: ReadStats) -> None:
            progress.update(task, completed=min(current.chars, size), lines=current.lines, accepted=current.accepted)

        total = database.stage_items(job.id, iter_candidates(wordlist, options, stats=stats, on_progress=report))
    database.mark_staged(job.id, total, stats.as_dict())
    refreshed = database.get_job(job.id)
    assert refreshed is not None
    return refreshed


def _provider_label(plan: ScanPlan) -> Text:
    if plan.provider_name == "demo":
        return Text.assemble(("Demo (offline)", "bold"), ("  simulated results, no network", MUTED))
    label = Text.assemble(("Minecraft", "bold"), (f"  bulk lookup, {BULK_LIMIT} names/request", MUTED))
    label.append("  · token verification" if plan.verify else "  · unverified", style="green" if plan.verify else MUTED)
    if plan.namemc:
        label.append("  · NameMC enrichment", style="yellow")
    return label


def _rate_label(cfg: AppConfig, provider_name: str) -> Text:
    requested = cfg.scanner.requests_per_second
    if provider_name == "demo":
        return Text(f"{requested:g} req/s (simulated)")
    effective = min(requested, DOCUMENTED_IP_LIMIT_RPS)
    text = Text(f"{effective:.2f} req/s")
    text.append(f"  ≈{effective * BULK_LIMIT:.0f} names/s max", style=MUTED)
    if requested > effective:
        text.append(f"\ncapped from {requested:g} — Mojang allows ~200 requests / 2 min per IP", style="yellow")
    return text


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}" + ("" if count == 1 else "s")


def _lookup_requests(provider_name: str, names: int) -> int:
    return -(-names // max(1, PROVIDERS[provider_name].max_batch_size))


def lookup_seconds(cfg: AppConfig, provider_name: str, names: int) -> float:
    """How long looking up ``names`` takes at the configured request rate (capped at Mojang's limit)."""
    rate = cfg.scanner.requests_per_second if provider_name == "demo" else min(cfg.scanner.requests_per_second, DOCUMENTED_IP_LIMIT_RPS)
    return _lookup_requests(provider_name, names) / rate


def _estimate(cfg: AppConfig, plan: ScanPlan, uncached: int) -> Text:
    """Rough duration for the names that actually need a request."""
    if uncached == 0:
        return Text("no requests needed — everything remaining is cached", style="green")
    requests = _lookup_requests(plan.provider_name, uncached)
    text = Text(f"{_plural(uncached, 'name')} to look up ≈ {_plural(requests, 'request')}")
    text.append(f" · ~{format_duration(lookup_seconds(cfg, plan.provider_name, uncached))}", style=MUTED)
    if plan.verify:
        text.append(" + slow verification of unclaimed names", style="yellow")
    return text


def _simple_banner(wordlist: Path, total: int, done: int, uncached: int, cfg: AppConfig, plan: ScanPlan) -> Panel:
    """The startup panel without technical details, for the menu."""
    remaining = total - done
    names = Text(f"{remaining:,}", style="bold")
    if done:
        names.append(f"  (continuing: {done:,} of {total:,} already done)", style=MUTED)
    if uncached == 0:
        time_needed = Text("none: everything is already checked", style="green")
    else:
        time_needed = Text(f"about {format_duration(lookup_seconds(cfg, plan.provider_name, uncached))}", style="bold")
        time_needed.append("  (Mojang's speed limit)", style=MUTED)
    rows: list[tuple[str, Text | str]] = [
        ("Word list", Text(wordlist.name, style="bold")),
        ("Names to check", names),
        ("Time needed", time_needed),
    ]
    if keepawake.supported() and plan.keep_awake:
        rows.append(("Sleep", Text("your PC stays awake while this runs", style="green")))
    rows.append(("Stop any time", "press Ctrl+C; everything found so far is saved"))
    rows.append(("Results", f"{plan.output_dir / 'available.txt'}"))
    return banner(rows)


def _scan_banner(
    wordlist: Path, job: JobRecord, total: int, done: int, uncached: int, cfg: AppConfig, plan: ScanPlan, db_path: Path
) -> Panel:
    stats = job.read_stats
    duplicates = max(stats.get("accepted", total) - total, 0)
    loaded = Text(f"{total:,}", style="bold")
    loaded.append(
        f"  ({_plural(stats.get('lines', 0), 'line')} · {stats.get('invalid', 0):,} invalid · "
        f"{stats.get('filtered', 0):,} filtered · {_plural(duplicates, 'duplicate')})",
        style=MUTED,
    )
    remaining = total - done
    if done == 0:
        progress = Text("new scan", style=MUTED)
    else:
        progress = Text.assemble(("resuming", "bold yellow"), (f"  {done:,} done · {remaining:,} remaining", MUTED))
    if plan.force:
        cache = Text("ignored (--force)", style="yellow")
    else:
        cache = Text(f"TTL {format_ttl(cfg.cache.ttl)}")
        if remaining - uncached > 0:
            cache.append(f"  · {remaining - uncached:,} of the remaining names cached", style="green")
    rows = [
        ("Word list", Text(wordlist.name, style="bold")),
        ("Words loaded", loaded),
        ("Progress", progress),
        ("Estimate", _estimate(cfg, plan, uncached)),
        ("Provider", _provider_label(plan)),
        ("Workers", str(cfg.scanner.workers)),
        ("Rate", _rate_label(cfg, plan.provider_name)),
        ("Cache", cache),
        ("Database", str(db_path)),
        ("Output", f"{plan.output_dir}{os.sep}"),
    ]
    if keepawake.supported():
        rows.append(
            (
                "Keep awake",
                Text("on: the PC will not go to sleep while this runs", style="green")
                if plan.keep_awake
                else Text("off (--allow-sleep)", style=MUTED),
            )
        )
    return banner(rows)


def _output_files(writer: ResultWriter) -> list[str]:
    paths = [writer.available_path, writer.soon_path, writer.jsonl_path]
    return [str(path) for path in paths if path is not None and path.exists()]


async def _scan_async(
    console: Console, cfg: AppConfig, plan: ScanPlan, database: Database, job_id: str, state: ScanState
) -> tuple[ScanOutcome | None, RequestStats, list[str]]:
    restore_break_handler = _install_break_handler(asyncio.current_task())
    writer = ResultWriter(plan.output_dir, jsonl=cfg.output.jsonl)
    enrichers: list[Provider] = []
    try:
        async with build_provider(plan.provider_name, cfg, verify=plan.verify, token=plan.token) as provider:
            if plan.self_test:
                with console.status("Provider self-test (1 request)…", spinner=spinner_name()):
                    try:
                        await provider.self_test()
                    except ProviderSelfTestError as exc:
                        console.print(f"[bold red]Self-test failed:[/] {exc}")
                        return None, provider.request_stats, []
                console.print(f"{mark()} Provider self-test passed")
            if plan.namemc:
                enricher = build_provider("namemc", cfg)
                await enricher.start()
                if enricher.enabled:
                    enrichers.append(enricher)
                    console.print(f"{mark()} NameMC enrichment enabled (robots.txt permits /search)")
                else:
                    console.print(f"[yellow]NameMC enrichment disabled:[/] {enricher.disabled_reason}")
                    await enricher.close()

            scanner = Scanner(
                db=database,
                job_id=job_id,
                provider=provider,
                state=state,
                options=ScanOptions(
                    workers=cfg.scanner.workers,
                    cache_ttl=cfg.cache.ttl,
                    force=plan.force,
                    filter_offensive=cfg.scanner.filter_offensive,
                ),
                writer=writer,
                enrichers=enrichers,
            )
            if plan.live:
                dashboard = ScanDashboard(state, provider, workers=cfg.scanner.workers)
                with Live(dashboard, console=console, refresh_per_second=4, transient=False):
                    outcome = await scanner.run()
            else:
                reporter = PlainReporter(console, state)
                scanner.on_result = reporter.on_result
                ticker = asyncio.create_task(reporter.run())
                try:
                    outcome = await scanner.run()
                finally:
                    ticker.cancel()
                    await asyncio.gather(ticker, return_exceptions=True)
            stats = RequestStats.merged(provider.request_stats, *(e.request_stats for e in enrichers))
            return outcome, stats, _output_files(writer)
    finally:
        for enricher in enrichers:
            await enricher.close()
        writer.close()
        restore_break_handler()


@app.command()
def scan(
    ctx: typer.Context,
    wordlist: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="Word list: one word per line.", show_default=False)
    ],
    min_length: Annotated[int | None, typer.Option(min=1, max=16, help="Minimum username length.", rich_help_panel="Filters")] = None,
    max_length: Annotated[int | None, typer.Option(min=1, max=16, help="Maximum username length.", rich_help_panel="Filters")] = None,
    starts_with: Annotated[str | None, typer.Option(help="Keep names starting with this text.", rich_help_panel="Filters")] = None,
    ends_with: Annotated[str | None, typer.Option(help="Keep names ending with this text.", rich_help_panel="Filters")] = None,
    contains: Annotated[str | None, typer.Option(help="Keep names containing this text.", rich_help_panel="Filters")] = None,
    regex: Annotated[str | None, typer.Option(help="Keep names matching this regular expression.", rich_help_panel="Filters")] = None,
    exclude_regex: Annotated[str | None, typer.Option(help="Drop names matching this regular expression.", rich_help_panel="Filters")] = None,
    prefix: Annotated[str, typer.Option(help="Text prepended to every word.", rich_help_panel="Transform")] = "",
    suffix: Annotated[str, typer.Option(help="Text appended to every word.", rich_help_panel="Transform")] = "",
    transform: Annotated[
        Transform, typer.Option(case_sensitive=False, help="none | lowercase | compact (drops spaces, apostrophes, hyphens, accents).", rich_help_panel="Transform")
    ] = Transform.LOWERCASE,
    workers: Annotated[int | None, typer.Option(min=1, max=64, help="Concurrent workers (requests still obey the rate limit).", rich_help_panel="Network")] = None,
    requests_per_second: Annotated[
        float | None, typer.Option("--requests-per-second", "--rps", min=0.01, max=50, help="Global request budget (capped at Mojang's documented limit).", rich_help_panel="Network")
    ] = None,
    timeout: Annotated[float | None, typer.Option(min=1, max=120, help="Per-request timeout in seconds.", rich_help_panel="Network")] = None,
    max_retries: Annotated[int | None, typer.Option(min=0, max=10, help="Retries for timeouts, 5xx and 429 responses.", rich_help_panel="Network")] = None,
    provider: Annotated[ScanProvider | None, typer.Option(case_sensitive=False, help="Primary provider (default: from config, minecraft).", rich_help_panel="Network")] = None,
    verify: Annotated[
        bool | None, typer.Option("--verify/--no-verify", help="Confirm unclaimed names with Mojang's token-backed availability check (slow: 20 / 5 min).", rich_help_panel="Network")
    ] = None,
    namemc: Annotated[bool | None, typer.Option("--namemc/--no-namemc", help="Ask NameMC about unclaimed names for release estimates (experimental).", rich_help_panel="Network")] = None,
    cache_ttl: Annotated[str | None, typer.Option(help="Re-use results younger than this, e.g. 24h, 30m, 7d.", rich_help_panel="Cache & progress")] = None,
    force: Annotated[bool, typer.Option("--force", help="Ignore cached results and re-check everything.", rich_help_panel="Cache & progress")] = False,
    restart: Annotated[bool, typer.Option("--restart", help="Start this scan over instead of resuming.", rich_help_panel="Cache & progress")] = False,
    db: DbOption = None,
    output_dir: OutputDirOption = None,
    plain: Annotated[bool, typer.Option("--plain", help="Line-based output instead of the live dashboard.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Read and stage the word list, then stop (no requests).")] = False,
    skip_self_test: Annotated[bool, typer.Option("--skip-self-test", help="Skip the one-request endpoint self-test.")] = False,
    allow_sleep: Annotated[bool, typer.Option("--allow-sleep", help="Let the computer sleep during the scan (it is kept awake by default).")] = False,
    confirm: Annotated[bool, typer.Option("--confirm", help="Show the plan and ask before sending any requests.")] = False,
    simple: Annotated[bool, typer.Option("--simple", hidden=True, help="Plain-language startup panel (used by the menu).")] = False,
) -> None:
    """Scan a word list for available and soon-to-be-available usernames."""
    app_ctx = _app(ctx)
    console = app_ctx.console
    try:
        cfg = _with_overrides(
            app_ctx.config,
            workers=workers,
            requests_per_second=requests_per_second,
            timeout=timeout,
            max_retries=max_retries,
            cache_ttl=cache_ttl,
        )
        name_filter = CandidateFilter(
            min_length=min_length,
            max_length=max_length,
            starts_with=starts_with,
            ends_with=ends_with,
            contains=contains,
            regex=regex,
            exclude_regex=exclude_regex,
        )
    except re.error as exc:
        _fail(console, f"invalid regular expression: {exc}")
    except (ValueError, ConfigError) as exc:
        _fail(console, str(exc))

    provider_name = provider.value if provider else cfg.providers.default
    if provider_name not in SCAN_PROVIDERS:
        _fail(console, f"provider {provider_name!r} cannot drive a scan (choose: {', '.join(SCAN_PROVIDERS)})")
    is_demo = provider_name == "demo"
    use_verify = bool(verify if verify is not None else cfg.providers.minecraft.verify_availability) and not is_demo
    token = _read_token(cfg) if use_verify else None
    if use_verify and token is None:
        _fail(console, f"--verify needs a Minecraft access token in the ${cfg.providers.minecraft.token_env} environment variable")
    use_namemc = bool(namemc if namemc is not None else cfg.providers.namemc.enabled) and not is_demo

    db_path, out_dir = _paths(cfg, provider_name, db, output_dir)
    options = CandidateOptions(transform=transform, prefix=prefix, suffix=suffix, filter=name_filter)
    job_id = wordlist_fingerprint(wordlist, options)
    plan = ScanPlan(
        provider_name=provider_name,
        verify=use_verify,
        token=token,
        namemc=use_namemc,
        output_dir=out_dir,
        live=not plain and console.is_terminal,
        self_test=cfg.providers.minecraft.self_test and not skip_self_test and not is_demo,
        force=force,
        keep_awake=not allow_sleep,
    )
    logger.info("scan %s: wordlist=%s options=%s provider=%s", job_id, wordlist, options.as_dict(), provider_name)

    lock = ScanLock(lock_path_for(db_path))
    if not lock.acquire():
        _fail(
            console,
            "another search is already running (maybe in another window). Only one search can run at a "
            "time, because they would share Mojang's speed limit. Viewing results is fine meanwhile.",
            code=1,
        )
    with lock, Database(db_path) as database:
        _tidy(cfg, database)
        job = database.get_job(job_id) or database.create_job(job_id, str(wordlist.resolve()), options.as_dict())
        if not job.staged:
            try:
                job = _stage(console, database, job, wordlist, options)
            except KeyboardInterrupt:
                console.print("[yellow]Interrupted while reading the word list.[/] It will be read again next time.")
                raise typer.Exit(EXIT_INTERRUPTED) from None
        elif restart:
            database.reset_job(job_id)
        total, done = database.job_progress(job_id)
        if total == 0:
            console.print("[yellow]No usable usernames in this word list with the current filters.[/]")
            raise typer.Exit(1)

        uncached = (
            total - done
            if force
            else database.count_uncached_pending(job_id, max_age=cfg.cache.ttl, provider=provider_name)
        )
        if uncached == 0 and plan.self_test:
            plan = replace(plan, self_test=False)  # nothing will be requested, so nothing to test
        if simple:
            console.print(_simple_banner(wordlist, total, done, uncached, cfg, plan))
        else:
            console.print(_scan_banner(wordlist, job, total, done, uncached, cfg, plan, db_path))
        if done >= total:
            counts, confirmed = database.job_status_counts(job_id)
            console.print("[green]This scan is already complete.[/] Use [bold]--restart[/] to scan again (fresh cache entries are reused).")
            console.print(status_overview(counts, confirmed))
            return
        if dry_run:
            console.print("[bright_black]Dry run: word list staged, no requests sent.[/]")
            return
        if confirm and not Confirm.ask("Start now?", default=True, console=console):
            if done == 0:
                database.delete_job(job_id)  # never started: don't leave a "paused" search behind
            console.print("Not started. Nothing was sent.")
            return

        counts, confirmed = database.job_status_counts(job_id)
        state = ScanState(total=total, done_at_start=done, counters=ScanCounters.from_counts(counts, confirmed))
        try:
            with keepawake.keep_awake(plan.keep_awake), keepawake.no_quick_edit():
                outcome, stats, files = asyncio.run(_scan_async(console, cfg, plan, database, job_id, state))
        except KeyboardInterrupt:
            console.print("[yellow]Stopped before scanning started.[/] Nothing was lost.")
            raise typer.Exit(EXIT_INTERRUPTED) from None
        if outcome is None:
            raise typer.Exit(1)
        console.print(scan_summary(state, outcome, stats, output_files=files))
        if outcome.interrupted:
            raise typer.Exit(EXIT_INTERRUPTED)


# -- check -----------------------------------------------------------------------------------


async def _check_async(
    cfg: AppConfig, provider_name: str, names: list[str], *, verify: bool, token: str | None, namemc: bool
) -> list[CheckResult]:
    async with build_provider(provider_name, cfg, verify=verify, token=token) as provider:
        if not provider.enabled:
            return [
                CheckResult(
                    username=name,
                    status=Status.UNKNOWN,
                    provider=provider_name,
                    detail=f"{provider.display_name} disabled: {provider.disabled_reason}",
                )
                for name in names
            ]
        results: list[CheckResult] = []
        size = max(1, provider.max_batch_size)
        for start in range(0, len(names), size):
            results.extend(await provider.check_many(names[start : start + size]))
    if namemc:
        async with build_provider("namemc", cfg) as enricher:
            for index, result in enumerate(results):
                if enricher.enabled and result.status is Status.AVAILABLE and result.confidence is not Confidence.CONFIRMED:
                    results[index] = combine_with_enrichment(result, await enricher.check(result.username))
    return results


@app.command()
def check(
    ctx: typer.Context,
    usernames: Annotated[list[str], typer.Argument(help="One or more usernames.", show_default=False)],
    force: Annotated[bool, typer.Option("--force", "-f", help="Ignore cached results.")] = False,
    provider: Annotated[CheckProvider | None, typer.Option(case_sensitive=False, help="Provider to ask (default: from config).")] = None,
    verify: Annotated[
        bool | None, typer.Option("--verify/--no-verify", help="Confirm claimability with Mojang's token-backed check (default: on when the token variable is set).")
    ] = None,
    namemc: Annotated[bool | None, typer.Option("--namemc/--no-namemc", help="Ask NameMC about unclaimed names (experimental).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON lines instead of panels.")] = False,
    db: DbOption = None,
    output_dir: OutputDirOption = None,
) -> None:
    """Check one or more usernames right now."""
    app_ctx = _app(ctx)
    console = app_ctx.console
    cfg = app_ctx.config
    provider_name = provider.value if provider else cfg.providers.default
    token = _read_token(cfg)
    if verify and provider_name != "minecraft":
        _fail(console, "--verify only applies to the minecraft provider")
    use_verify = bool(verify if verify is not None else token is not None) and provider_name == "minecraft"
    if use_verify and token is None:
        _fail(console, f"--verify needs a Minecraft access token in the ${cfg.providers.minecraft.token_env} environment variable")
    use_namemc = bool(namemc if namemc is not None else cfg.providers.namemc.enabled) and provider_name == "minecraft"

    entries: list[tuple[str, str | None]] = []  # (name, validation error)
    names: list[str] = []
    seen: set[str] = set()
    for raw in usernames:
        name = raw.strip()
        validation = validate_username(name)
        if not validation.valid:
            entries.append((name, validation.message))
        elif name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
            entries.append((name, None))

    results: dict[str, CheckResult] = {}
    if names:
        db_path, out_dir = _paths(cfg, provider_name, db, output_dir)
        with Database(db_path) as database:
            _tidy(cfg, database)
            cached = {} if force else database.get_fresh(names, max_age=cfg.cache.ttl, provider=provider_name)
            if use_verify:  # an unverified cache entry is not good enough when verification is requested
                cached = {
                    key: r for key, r in cached.items()
                    if not (r.status is Status.AVAILABLE and r.confidence is not Confidence.CONFIRMED)
                }
            for result in cached.values():
                result.from_cache = True
            results.update(cached)
            pending = [name for name in names if name.lower() not in cached]
            if pending:
                spinner = (
                    contextlib.nullcontext()
                    if as_json
                    else console.status(f"Checking {len(pending)} name(s) with {provider_display_name(provider_name)}…", spinner=spinner_name())
                )
                try:
                    with spinner:
                        fresh = asyncio.run(
                            _check_async(cfg, provider_name, pending, verify=use_verify, token=token, namemc=use_namemc)
                        )
                except KeyboardInterrupt:
                    console.print("[yellow]Cancelled.[/]")
                    raise typer.Exit(EXIT_INTERRUPTED) from None
                for index, result in enumerate(fresh):
                    previous = database.get_result(result.username)
                    source = previous.source_word if previous else None
                    result.source_word = source
                    result.quality_score = quality_score(
                        result.username, dictionary_word=is_dictionary_word(result.username, source)
                    )
                    if cfg.scanner.filter_offensive:
                        fresh[index] = flag_if_offensive(result, source)
                database.upsert_results(fresh)
                database.commit()
                with ResultWriter(out_dir, jsonl=cfg.output.jsonl) as writer:
                    for result in fresh:
                        writer.record(result, fresh=True)
                results.update({result.key: result for result in fresh})

    for name, error in entries:
        if error is not None:
            if as_json:
                typer.echo(json.dumps({"username": name, "status": "invalid", "error": error}))
            else:
                console.print(invalid_panel(name, error))
            continue
        result = results[name.lower()]
        if as_json:
            typer.echo(json.dumps({**result.to_json(), "from_cache": result.from_cache}, ensure_ascii=False))
        else:
            console.print(result_panel(result))


# -- generate --------------------------------------------------------------------------------


@app.command()
def generate(
    ctx: typer.Context,
    length: Annotated[int, typer.Argument(min=min(SHORT_NAME_LENGTHS), max=max(SHORT_NAME_LENGTHS), help="Name length: 3 or 4.", show_default=False)],
    kind: Annotated[
        ShortNameKind,
        typer.Argument(case_sensitive=False, help="letters (a-z only) | numbers (at least one digit) | underscores (at least one _)."),
    ] = ShortNameKind.LETTERS,
) -> None:
    """Write a word list of every possible 3- or 4-character name, ready to scan."""
    app_ctx = _app(ctx)
    path = short_name_wordlist(length, kind)
    count = short_name_count(length, kind)
    app_ctx.console.print(f"{mark()} {count:,} names in [bold]{path}[/]")
    seconds = lookup_seconds(app_ctx.config, "minecraft", count)
    app_ctx.console.print(f"Scan them with [bold]minecraft-finder scan {path}[/] (about {format_duration(seconds)}).")


# -- stats -----------------------------------------------------------------------------------


def _options_summary(options: dict[str, object]) -> str:
    parts = [f"transform={options.get('transform', 'lowercase')}"]
    if options.get("prefix"):
        parts.append(f"prefix={options['prefix']}")
    if options.get("suffix"):
        parts.append(f"suffix={options['suffix']}")
    filters = options.get("filter") or {}
    if isinstance(filters, dict):
        parts.extend(f"{key.replace('_', '-')}={value}" for key, value in filters.items())
    return " ".join(parts)


@app.command()
def stats(ctx: typer.Context, db: DbOption = None, demo: DemoOption = False) -> None:
    """Show result totals and scan jobs."""
    app_ctx = _app(ctx)
    console = app_ctx.console
    db_path = _db_path(app_ctx.config, db, demo)
    if not db_path.exists():
        console.print(f"[yellow]No database at {db_path} yet.[/] Run a scan or a check first.")
        return
    with Database(db_path) as database:
        _tidy(app_ctx.config, database)
        counts = database.status_counts()
        confirmed = database.confirmed_available()
        oldest, newest = database.check_time_bounds()
        jobs = database.list_jobs()
    footer = Text(f"{db_path}", style=MUTED)
    if newest is not None:
        footer.append(f"  ·  last check {humanize_ago(newest)}  ·  oldest {humanize_ago(oldest)}", style=MUTED)
    console.print(
        Panel(
            Group(status_overview(counts, confirmed), footer),
            title=Text(" Results database ", style=f"bold {ACCENT}"),
            border_style=ACCENT,
            box=box.ROUNDED,
            expand=False,
            padding=(0, 2),
        )
    )
    if not jobs:
        return
    table = Table(title="Scan jobs", title_justify="left", box=box.SIMPLE_HEAD, header_style=MUTED)
    table.add_column("Job", style=MUTED)
    table.add_column("Word list", style="bold")
    table.add_column("Options", style=MUTED, overflow="fold")
    table.add_column("Progress", justify="right")
    table.add_column("State")
    table.add_column("Updated", style=MUTED)
    for summary in jobs:
        job = summary.job
        if job.finished_at:
            state_text = Text("finished", style="green")
        elif not job.staged:
            state_text = Text("staging", style="yellow")
        else:
            state_text = Text("resumable", style="yellow")
        table.add_row(
            job.id[:8],
            Path(job.wordlist).name,
            _options_summary(job.options),
            f"{summary.done:,} / {job.total:,} ({percent(summary.done, job.total)})",
            state_text,
            humanize_ago(job.updated_at),
        )
    console.print(table)


# -- export ----------------------------------------------------------------------------------

EXPORT_COLUMNS = (
    "username", "status", "confidence", "quality_score", "available_at",
    "checked_at", "provider", "uuid", "detail", "error",
)  # fmt: skip


def _write_export(results: Iterable[CheckResult], fmt: ExportFormat, handle: IO[str], preview: list[CheckResult]) -> int:
    count = 0
    if fmt is ExportFormat.csv:
        writer = csv.writer(handle)
        writer.writerow(EXPORT_COLUMNS)
    elif fmt is ExportFormat.json:
        handle.write("[")
    for result in results:
        record = result.to_json()
        if fmt is ExportFormat.csv:
            writer.writerow([record[column] if record[column] is not None else "" for column in EXPORT_COLUMNS])
        elif fmt is ExportFormat.json:
            handle.write(("\n  " if count == 0 else ",\n  ") + json.dumps(record, ensure_ascii=False))
        elif fmt is ExportFormat.jsonl:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        else:
            handle.write(result.name + "\n")
        if len(preview) < 10:
            preview.append(result)
        count += 1
    if fmt is ExportFormat.json:
        handle.write("\n]\n" if count else "]\n")
    return count


@app.command()
def export(
    ctx: typer.Context,
    status: Annotated[
        list[StatusChoice] | None,
        typer.Option("--status", "-s", case_sensitive=False, help="Statuses to include (repeatable) (default: available, soon); 'all' for everything."),
    ] = None,
    sort: Annotated[
        SortKey, typer.Option(case_sensitive=False, help="Sort order; best = confirmed names first, then by score. Sorting never hides names.")
    ] = SortKey.best,
    fmt: Annotated[ExportFormat, typer.Option("--format", "-F", case_sensitive=False, help="Output format.")] = ExportFormat.csv,
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Destination file, '-' for stdout (default: <output dir>/export.<format>).")] = None,
    limit: Annotated[int | None, typer.Option(min=1, help="Export at most this many names.")] = None,
    min_score: Annotated[float | None, typer.Option(min=0, max=100, help="Only names with at least this quality score.")] = None,
    confirmed_only: Annotated[bool, typer.Option("--confirmed-only", help="Exclude unverified availability.")] = False,
    db: DbOption = None,
    demo: DemoOption = False,
) -> None:
    """Export results from the database (CSV, JSON, JSON Lines or plain text)."""
    app_ctx = _app(ctx)
    console = app_ctx.console
    cfg = app_ctx.config
    db_path = _db_path(cfg, db, demo)
    if not db_path.exists():
        _fail(console, f"no database at {db_path} yet — run a scan first", code=1)
    chosen = set(status or [StatusChoice.available, StatusChoice.soon])
    statuses = None if StatusChoice.all in chosen else {Status(choice.value.upper()) for choice in chosen}
    preview: list[CheckResult] = []
    with Database(db_path) as database:
        _tidy(cfg, database)
        results = database.iter_results(
            statuses=statuses, sort=sort.value, limit=limit, min_score=min_score, confirmed_only=confirmed_only
        )
        if output is not None and str(output) == "-":
            _write_export(results, fmt, sys.stdout, preview)
            return
        destination = output or Path(cfg.output.directory) / ("demo" if demo else "") / f"export.{fmt.value}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary file first so an interrupted export never leaves a half-written file.
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=destination.parent, prefix=".export-", suffix=".tmp", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            try:
                count = _write_export(results, fmt, handle, preview)
            except BaseException:
                handle.close()
                temp_path.unlink(missing_ok=True)
                raise
        os.replace(temp_path, destination)
    if preview:
        console.print(results_preview(preview, title=f"Top {len(preview)} of {count:,} (sorted by {sort.value})"))
    console.print(f"{mark()} Exported {count:,} names to [bold]{destination}[/]")


# -- providers -------------------------------------------------------------------------------


async def _provider_tests(cfg: AppConfig, console: Console) -> None:
    async with build_provider("minecraft", cfg) as minecraft:
        try:
            await minecraft.self_test()
            console.print(f"{mark()} Minecraft: lookup endpoint behaves as expected (1 request)")
        except ProviderSelfTestError as exc:
            console.print(f"{mark(False)} Minecraft: {exc}")
    if cfg.providers.namemc.enabled:
        async with build_provider("namemc", cfg) as namemc:
            if namemc.enabled:
                console.print(f"{mark()} NameMC: robots.txt permits /search")
            else:
                console.print(f"{mark(False)} NameMC: {namemc.disabled_reason}")


def _endpoint_text(endpoints: Iterable[str]) -> Text:
    """Group endpoint URLs by host so long URLs stay readable in narrow columns."""
    text = Text()
    last_host = None
    for endpoint in endpoints:
        url, _, note = endpoint.partition(" ")
        parts = urlsplit(url)
        if parts.netloc != last_host:
            if text:
                text.append("\n")
            text.append(parts.netloc, style="bold")
            last_host = parts.netloc
        text.append(f"\n  {parts.path}" + (f"?{parts.query}" if parts.query else ""), style=MUTED)
        if note:
            text.append(f" {note}", style="yellow")
    return text if text else Text("—", style=MUTED)


@app.command("providers")
def providers_command(
    ctx: typer.Context,
    test: Annotated[bool, typer.Option("--test", help="Send one self-test request to Minecraft (and fetch NameMC's robots.txt if enabled).")] = False,
) -> None:
    """List the available providers and their limits."""
    app_ctx = _app(ctx)
    console = app_ctx.console
    cfg = app_ctx.config
    token = _read_token(cfg)
    table = Table(title="Providers", title_justify="left", box=box.ROUNDED, header_style=f"bold {ACCENT}", show_lines=True)
    table.add_column("Provider", no_wrap=True)
    table.add_column("Status")
    table.add_column("Rate limits", min_width=24)
    table.add_column("Endpoints", overflow="fold")
    notes: list[Text] = []
    for name, cls in PROVIDERS.items():
        info = cls.info(cfg)
        if name == "minecraft":
            status = Text("ready", style="green")
            status.append(
                f"\n${cfg.providers.minecraft.token_env} set:\n--verify available" if token else "\nno token: results\nunverified",
                style=MUTED,
            )
        elif name == "namemc":
            status = Text("enabled\n(experimental)", style="yellow") if cfg.providers.namemc.enabled else Text("off (opt-in)", style=MUTED)
        else:
            status = Text("ready (offline)", style="green")
        provider = Text(info.display_name, style="bold")
        provider.append(f"\n{info.role.value}", style=MUTED)
        table.add_row(provider, status, info.rate_limit, _endpoint_text(info.endpoints))
        notes.append(Text.assemble((f"{info.display_name}: ", "bold"), (info.description, MUTED)))
    console.print(table)
    for note in notes:
        console.print(note)
    if test:
        asyncio.run(_provider_tests(cfg, console))


# -- verify ----------------------------------------------------------------------------------

TOKEN_HELP = """[bold]How to get your Minecraft access token[/] (it works for about 24 hours):
  1. In Chrome or Edge, open minecraft.net, sign in, and open the page where you
     change your Java profile name.
  2. Press F12, click the "Network" tab, then press F5 to reload the page.
  3. Type [bold]minecraftservices[/] into the filter box of the Network tab.
  4. Click any request in the list (for example "profile").
  5. Under "Request Headers", find [bold]authorization: Bearer eyJ…[/]
  6. Copy everything after "Bearer " and paste it below.
[yellow]Treat it like a password:[/] for about a day it lets anyone act as your Minecraft account.
This tool only sends it to Mojang (api.minecraftservices.com) and never saves it."""


def verify_per_minute(cfg: AppConfig) -> float:
    return min(cfg.providers.minecraft.verify_requests_per_minute, DOCUMENTED_AVAILABILITY_LIMIT_RPS * 60)


def clean_token(raw: str) -> str | None:
    """Accept the token with or without quotes and a leading "Bearer"."""
    token = raw.strip().strip("\"'").strip()
    if token[:6].lower() == "bearer":
        token = token[6:].strip()
    return token or None


def ask_token(console: Console, cfg: AppConfig) -> str | None:
    """The token from the environment, or pasted by the user (hidden input)."""
    token = _read_token(cfg)
    if token is not None:
        return token
    console.print(Panel(TOKEN_HELP, border_style=ACCENT, box=box.ROUNDED, padding=(1, 2)))
    return clean_token(Prompt.ask("Paste your token here, then press Enter (it stays invisible while you paste)", password=True, default="", show_default=False, console=console))


async def _verify_async(
    console: Console, cfg: AppConfig, database: Database, out_dir: Path, candidates: list[CheckResult], token: str
) -> tuple[Counter[str], list[CheckResult]]:
    tally: Counter[str] = Counter()
    confirmed: list[CheckResult] = []
    provider = MinecraftProvider(cfg.providers.minecraft, cfg.scanner, token=token, verify=True)
    progress = Progress(
        SpinnerColumn(spinner_name(), style=ACCENT),
        TextColumn("[bold]Confirming with Mojang"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    )
    async with provider:
        with ResultWriter(out_dir, jsonl=cfg.output.jsonl) as writer, progress:
            task = progress.add_task("verify", total=len(candidates))
            for candidate in candidates:
                result = await provider.verify_name(candidate.username)
                if provider.verify_disabled_reason:
                    tally["token rejected"] += 1
                    break
                progress.advance(task)
                if result.status is Status.AVAILABLE and result.confidence is not Confidence.CONFIRMED:
                    tally["could not check"] += 1  # e.g. rate-limited even after retries; stays unconfirmed
                    continue
                result.display_name = result.display_name or candidate.display_name
                result.source_word = candidate.source_word
                result.quality_score = candidate.quality_score
                database.upsert_result(result)
                database.commit()  # every answer is kept, even if the run is stopped
                writer.record(result)
                tally[result.status.value] += 1
                if result.status is Status.AVAILABLE:
                    confirmed.append(result)
                progress.console.print(discovery_line(result))
    return tally, confirmed


def run_verify(
    app_ctx: AppContext, *, top: int, token: str, min_score: float | None = None, max_length: int | None = None
) -> int:
    """Confirm the best unconfirmed "likely available" names with Mojang's own check. Returns an exit code."""
    console, cfg = app_ctx.console, app_ctx.config
    db_path, out_dir = _paths(cfg, "minecraft", None, None)
    if not db_path.exists():
        console.print("[yellow]There are no results yet.[/] Run a search first.")
        return 1
    with Database(db_path) as database:
        _tidy(cfg, database)
        candidates = list(
            database.iter_results(
                statuses={Status.AVAILABLE},
                sort="quality",
                limit=top,
                min_score=min_score,
                max_length=max_length,
                unverified_only=True,
            )
        )
        if not candidates:
            console.print("Nothing to confirm: every 'likely available' name has already been checked.")
            return 0
        per_minute = verify_per_minute(cfg)
        console.print(
            f"Confirming your best [bold]{len(candidates):,}[/] names takes about "
            f"[bold]{format_duration(len(candidates) / per_minute * 60)}[/], because Mojang allows only "
            f"~{per_minute:g} checks per minute per account."
        )
        console.print(f"[{MUTED}]Press Ctrl+C to stop at any time; every answer is saved immediately.[/]\n")
        try:
            with keepawake.keep_awake(), keepawake.no_quick_edit():
                tally, confirmed = asyncio.run(_verify_async(console, cfg, database, out_dir, candidates, token))
        except KeyboardInterrupt:
            console.print("[yellow]Stopped.[/] The answers so far are saved; run it again to continue with the rest.")
            return EXIT_INTERRUPTED

    if tally["token rejected"]:
        console.print(
            f"{mark(False)} Mojang rejected the token. It may have expired (they last about 24 hours) "
            "or been copied incompletely. Get a fresh one and try again."
        )
        return 1
    rows = [
        ("Free (confirmed)", Text(f"{tally[Status.AVAILABLE.value]:,}", style="bold green")),
        ("On hold or locked", Text(f"{tally[Status.SOON.value]:,}", style="bold yellow")),
        ("Not allowed", f"{tally[Status.BLOCKED.value]:,}"),
        ("Taken", f"{tally[Status.TAKEN.value]:,}"),
    ]
    if tally["could not check"]:
        rows.append(("Could not check", Text(f"{tally['could not check']:,}  (try again later)", style="yellow")))
    console.print(banner(rows, title="Confirmed with Mojang"))
    if confirmed:
        best = sorted(confirmed, key=lambda r: -(r.quality_score or 0))[:20]
        console.print(results_preview(best, title="Free names you can claim now (best first)"))
        console.print(f"{mark()} All confirmed names are in [bold]{out_dir / 'confirmed.txt'}[/]. Claim them at minecraft.net.")
    return 0


@app.command()
def verify(
    ctx: typer.Context,
    top: Annotated[int, typer.Option("--top", min=1, max=5000, help="How many of your best unconfirmed names to check.")] = 100,
    min_score: Annotated[float | None, typer.Option(min=0, max=100, help="Only check names with at least this score.")] = None,
    max_length: Annotated[int | None, typer.Option(min=3, max=16, help="Only check names with at most this many characters, e.g. 4.")] = None,
) -> None:
    """Confirm your best "likely available" names with Mojang's own check (needs your Minecraft login token)."""
    app_ctx = _app(ctx)
    token = ask_token(app_ctx.console, app_ctx.config)
    if token is None:
        _fail(app_ctx.console, "no token given; nothing was checked")
    code = run_verify(app_ctx, top=top, token=token, min_score=min_score, max_length=max_length)
    if code:
        raise typer.Exit(code)


# -- database maintenance --------------------------------------------------------------------


@database_app.command("clean")
def database_clean(
    ctx: typer.Context,
    older_than: Annotated[str | None, typer.Option(help="Also delete results last checked longer ago than this, e.g. 30d.")] = None,
    errors: Annotated[bool, typer.Option("--errors/--keep-errors", help="Delete ERROR and UNKNOWN results (they are re-checked anyway).")] = True,
    jobs: Annotated[bool, typer.Option("--jobs/--keep-jobs", help="Delete progress data of finished scans.")] = True,
    all_jobs: Annotated[bool, typer.Option("--all-jobs", help="Also delete progress of unfinished scans (they will start over).")] = False,
    vacuum: Annotated[bool, typer.Option("--vacuum/--no-vacuum", help="Compact the database file afterwards.")] = True,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    db: DbOption = None,
    demo: DemoOption = False,
) -> None:
    """Remove stale or useless rows from the database."""
    app_ctx = _app(ctx)
    console = app_ctx.console
    db_path = _db_path(app_ctx.config, db, demo)
    if not db_path.exists():
        console.print(f"[yellow]No database at {db_path}; nothing to clean.[/]")
        return
    try:
        cutoff = utcnow() - parse_duration(older_than) if older_than else None
    except ValueError as exc:
        _fail(console, str(exc))

    size_before = db_path.stat().st_size
    with Database(db_path) as database:
        plan: list[tuple[str, int]] = []
        if errors:
            plan.append(("ERROR / UNKNOWN results", database.count_results(statuses={Status.ERROR, Status.UNKNOWN})))
        if cutoff is not None:
            plan.append((f"results checked before {cutoff:%Y-%m-%d %H:%M} UTC", database.count_results(checked_before=cutoff)))
        if jobs or all_jobs:
            label = "scan jobs (all, including unfinished)" if all_jobs else "finished scan jobs"
            plan.append((label, database.count_jobs(finished_only=not all_jobs)))
        table = Table(box=box.SIMPLE_HEAD, header_style=MUTED, title="Clean-up plan", title_justify="left")
        table.add_column("Remove")
        table.add_column("Rows", justify="right")
        for label, count in plan:
            table.add_row(label, f"{count:,}")
        console.print(table)
        if not any(count for _, count in plan) and not vacuum:
            console.print("Nothing to clean.")
            return
        if not yes:
            typer.confirm("Proceed?", abort=True)
        removed = 0
        if errors:
            removed += database.delete_results(statuses={Status.ERROR, Status.UNKNOWN})
        if cutoff is not None:
            removed += database.delete_results(checked_before=cutoff)
        removed_jobs = database.delete_jobs(finished_only=not all_jobs) if (jobs or all_jobs) else 0
        if vacuum:
            database.vacuum()
    size_after = db_path.stat().st_size
    console.print(
        f"{mark()} Removed {removed:,} results and {removed_jobs:,} scan jobs · "
        f"{size_before / 1024:,.0f} KiB → {size_after / 1024:,.0f} KiB"
    )


@database_app.command("info")
def database_info(ctx: typer.Context, db: DbOption = None, demo: DemoOption = False) -> None:
    """Show where the database lives and how big it is."""
    app_ctx = _app(ctx)
    console = app_ctx.console
    db_path = _db_path(app_ctx.config, db, demo)
    if not db_path.exists():
        console.print(f"[yellow]No database at {db_path} yet.[/]")
        return
    wal = db_path.with_name(db_path.name + "-wal")
    with Database(db_path) as database:
        rows = [
            ("Path", str(db_path.resolve())),
            ("Size", f"{db_path.stat().st_size / 1024:,.0f} KiB" + (f" (+{wal.stat().st_size / 1024:,.0f} KiB WAL)" if wal.exists() else "")),
            ("Schema", f"v{database.schema_version()}"),
            ("Results", f"{database.total_results():,}"),
            ("Scan jobs", f"{database.count_jobs(finished_only=False):,} ({database.count_jobs(finished_only=True):,} finished)"),
            ("Log file", str(app_ctx.log_file)),
        ]
    console.print(banner(rows, title="Database"))


def _utf8_when_redirected() -> None:
    """Redirected output on Windows defaults to a legacy code page that cannot encode ✓ or ◷."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None and not stream.isatty() and (stream.encoding or "").lower() not in ("utf-8", "utf8"):
            reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    _utf8_when_redirected()
    app()
