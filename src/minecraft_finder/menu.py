"""A simple numbered menu, so nobody has to remember commands or options.

Shown when ``minecraft-finder`` is started without arguments (for example by
double-clicking ``Start.bat``). Every action reuses the regular commands, so the
menu and the command line always behave the same way.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich import box
from rich.panel import Panel
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from . import APP_NAME, AUTHOR
from . import cli as commands
from .database import Database, JobSummary
from .models import Status
from .scanlock import scan_running
from .timeutil import format_duration, humanize_ago
from .ui import ACCENT, MUTED, mark, percent, results_preview, symbol
from .validator import MAX_LENGTH, MIN_LENGTH
from .wordlist import CandidateOptions, Transform, wordlist_fingerprint
from .wordsource import (
    ENGLISH_WORDS_APPROX,
    ENGLISH_WORDS_PATH,
    SHORT_NAME_LENGTHS,
    DownloadError,
    ShortNameKind,
    download_wordlist,
    sample_wordlist,
    short_name_count,
    short_name_length,
    short_name_wordlist,
)

Option = tuple[str, str, str]  # (key, label, hint)
_AFFIX_RE = re.compile(r"[A-Za-z0-9_]*")
# Sort orders offered by "show" and "save": menu key -> (label, hint, sort key).
_SORTS: dict[str, tuple[str, str, commands.SortKey]] = {
    "1": ("Best first", "confirmed free names first, then the nicest", commands.SortKey.best),
    "2": ("Shortest first", "", commands.SortKey.length),
    "3": ("A to Z", "", commands.SortKey.name),
    "4": ("Coming soon first", "earliest release date first", commands.SortKey.release),
}
# Kinds of short names offered by "every short name": menu key -> (label, hint, kind).
_SHORT_KINDS: dict[str, tuple[str, str, ShortNameKind]] = {
    "1": ("Only letters", "a–z", ShortNameKind.LETTERS),
    "2": ("With numbers", "at least one number", ShortNameKind.NUMBERS),
    "3": ("With underscores", "at least one _", ShortNameKind.UNDERSCORES),
}


class Menu:
    def __init__(self, ctx: typer.Context) -> None:
        self.ctx = ctx
        self.app: commands.AppContext = ctx.obj
        self.console = self.app.console
        self.config = self.app.config

    # -- small helpers -----------------------------------------------------------------------

    @property
    def is_demo(self) -> bool:
        return self.config.providers.default == "demo"

    @property
    def db_path(self) -> Path:
        return commands._paths(self.config, self.config.providers.default, None, None)[0]

    @property
    def output_dir(self) -> Path:
        return commands._paths(self.config, self.config.providers.default, None, None)[1]

    def choose(self, title: str, options: list[Option], *, default: str | None = None) -> str:
        self.console.print(Text(title, style="bold"))
        for key, label, hint in options:
            line = Text("   ")
            line.append(key, style=f"bold {ACCENT}")
            line.append(f"  {label}")
            if hint:
                line.append(f"   {hint}", style=MUTED)
            self.console.print(line)
        extra: dict[str, Any] = {} if default is None else {"default": default}  # no default: Enter re-asks
        return Prompt.ask(
            "Choose a number", choices=[key for key, _, _ in options], show_choices=False, console=self.console, **extra
        )

    def pick_sort(self) -> tuple[str, commands.SortKey]:
        choice = self.choose(
            "How should the names be sorted?", [(key, label, hint) for key, (label, hint, _) in _SORTS.items()], default="1"
        )
        self.console.print()
        label, _, sort = _SORTS[choice]
        return label, sort

    def pause(self) -> None:
        Prompt.ask(f"[{MUTED}]Press Enter to go back to the menu[/]", default="", show_default=False, console=self.console)

    def run(self, command: Callable[..., Any], **kwargs: Any) -> int:
        """Run one of the regular commands and return its exit code instead of exiting."""
        try:
            command(self.ctx, **kwargs)
        except typer.Exit as exc:
            return exc.exit_code
        except typer.Abort:
            return 1
        return 0

    def open_folder(self, path: Path) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError:
            self.console.print(f"Your results are in {path.resolve()}")

    def counts(self) -> dict[Status, int]:
        if not self.db_path.exists():
            return {}
        with Database(self.db_path) as database:
            commands._tidy(self.config, database)
            return database.status_counts()

    def scan_running(self) -> bool:
        return scan_running(self.db_path)

    def blocked_by_running_scan(self) -> bool:
        if not self.scan_running():
            return False
        self.console.print(
            "[yellow]A search is already running in another window.[/] Only one can run at a time, because "
            "they would share Mojang's speed limit. Wait until it finishes, or stop it with Ctrl+C there."
        )
        self.pause()
        return True

    def unfinished(self) -> list[JobSummary]:
        if not self.db_path.exists():
            return []
        with Database(self.db_path) as database:
            jobs = database.list_jobs()
        return [s for s in jobs if s.job.staged and s.job.finished_at is None and s.done < s.job.total]

    # -- main loop ---------------------------------------------------------------------------

    def loop(self) -> None:
        try:
            while True:
                self.console.clear()
                self.header()
                running = self.scan_running()
                unfinished = [] if running else self.unfinished()
                options: list[Option] = [
                    ("1", "Check if a name is free", ""),
                    ("2", "Search for free names", "words, or every 3- or 4-character name · can run overnight"),
                ]
                if unfinished:
                    options.append(("3", "Continue my unfinished search", f"{len(unfinished)} paused"))
                options += [
                    ("4", "Show the free names I found", ""),
                    ("5", "Save my results to a file", "Excel or text"),
                    ("6", "Confirm my best finds with Mojang", "free, coming soon or blocked? needs your login"),
                    ("7", "How does this work?", ""),
                    ("0", "Quit", ""),
                ]
                choice = self.choose("What do you want to do?", options)
                self.console.print()
                if choice == "0":
                    break
                actions = {
                    "1": self.check_names,
                    "2": self.search,
                    "3": self.resume,
                    "4": self.show_found,
                    "5": self.save_results,
                    "6": self.confirm_best,
                    "7": self.help,
                }
                actions[choice]()
        except (KeyboardInterrupt, EOFError):
            self.console.print()
        self.console.print("Bye!")

    def header(self) -> None:
        body = Text("Find Minecraft Java names that nobody is using.")
        counts = self.counts()
        if counts:
            body.append("\n\nFound so far:  ", style=MUTED)
            body.append(f"{symbol(Status.AVAILABLE)} {counts.get(Status.AVAILABLE, 0):,} free", style="bold green")
            body.append("   ")
            body.append(f"{symbol(Status.SOON)} {counts.get(Status.SOON, 0):,} coming soon", style="bold yellow")
            body.append(f"   ({sum(counts.values()):,} names checked)", style=MUTED)
        if self.scan_running():
            body.append("\n\nA search is running in another window.", style="bold yellow")
            body.append(" You can look at and save results here.", style="yellow")
        self.console.print(
            Panel(
                body,
                title=Text(f" {APP_NAME} ", style=f"bold {ACCENT}"),
                subtitle=Text(f" made by {AUTHOR} ", style=MUTED),
                subtitle_align="right",
                border_style=ACCENT,
                box=box.ROUNDED,
                padding=(1, 3),
                expand=False,
            )
        )
        self.console.print()

    # -- 1: check ----------------------------------------------------------------------------

    def check_names(self) -> None:
        self.console.print(Text("Type one or more names, separated by spaces.", style="bold"))
        raw = Prompt.ask("Names", default="", show_default=False, console=self.console)
        names = raw.replace(",", " ").split()
        if names:
            self.console.print()
            self.run(commands.check, usernames=names)
        self.pause()

    # -- 2: search ---------------------------------------------------------------------------

    def search(self) -> None:
        if self.blocked_by_running_scan():
            return
        choice = self.choose("Which names should I try?", self.wordlist_options(), default="1")
        if choice == "0":
            return
        if choice == "3":
            self.search_short_names()
            return
        wordlist = self.pick_wordlist(choice)
        if wordlist is None:
            return
        self.console.print()
        min_length, max_length = self.pick_lengths()
        self.console.print()
        prefix, suffix = self.pick_affixes()
        allow_digits = Confirm.ask(
            "Allow numbers and underscores in the words (like glacier_2)?", default=False, console=self.console
        )
        # "No numbers" applies to the word itself, not to letters the user chose to add.
        regex = None if allow_digits else f"^{re.escape(prefix)}[A-Za-z]+{re.escape(suffix)}$"
        self.console.print()
        code = self.run(
            commands.scan,
            wordlist=wordlist,
            transform=Transform.COMPACT,
            min_length=min_length,
            max_length=max_length,
            prefix=prefix,
            suffix=suffix,
            regex=regex,
            confirm=True,
            simple=True,
        )
        self.after_scan(code)

    def wordlist_options(self) -> list[Option]:
        hint = "already downloaded" if ENGLISH_WORDS_PATH.is_file() else "downloads once, 4 MB"
        options: list[Option] = [
            ("1", "English dictionary", f"about {ENGLISH_WORDS_APPROX:,} words · {hint}"),
            ("2", "My own word list", "a .txt file with one word per line"),
            ("3", "Every short name", "all possible 3- or 4-character names · no word list needed"),
        ]
        if sample_wordlist() is not None:
            options.append(("4", "Tiny sample list", "30 words · good for a first try"))
        options.append(("0", "Back", ""))
        return options

    def pick_wordlist(self, choice: str) -> Path | None:
        if choice == "1":
            return ENGLISH_WORDS_PATH if ENGLISH_WORDS_PATH.is_file() else self.download_english()
        if choice == "4":
            return sample_wordlist()
        return self.ask_for_file()

    def search_short_names(self) -> None:
        self.console.print()
        choice = self.choose(
            "How long should the names be?",
            [
                ("1", "3 characters", "the rarest · almost all are taken"),
                ("2", "4 characters", "takes much longer"),
                ("0", "Back", ""),
            ],
            default="1",
        )
        if choice == "0":
            return
        length = 3 if choice == "1" else 4
        self.console.print()
        provider = self.config.providers.default
        options: list[Option] = []
        for key, (label, hint, kind) in _SHORT_KINDS.items():
            count = short_name_count(length, kind)
            time_needed = format_duration(commands.lookup_seconds(self.config, provider, count))
            options.append((key, label, f"{hint} · {count:,} names · about {time_needed}"))
        options.append(("0", "Back", ""))
        self.console.print(f"[{MUTED}]The three never overlap, so together they cover every possible {length}-character name.[/]")
        choice = self.choose("Which characters?", options, default="1")
        if choice == "0":
            return
        with self.console.status("Preparing the list…"):
            wordlist = short_name_wordlist(length, _SHORT_KINDS[choice][2])
        self.console.print()
        code = self.run(commands.scan, wordlist=wordlist, confirm=True, simple=True)
        self.after_scan(code, short_length=length)

    def download_english(self) -> Path | None:
        self.console.print("Downloading the English word list (public domain, github.com/dwyl/english-words)…")
        progress = Progress(
            TextColumn("[bold]Downloading"), BarColumn(), DownloadColumn(), TransferSpeedColumn(), console=self.console, transient=True
        )
        try:
            with progress:
                task = progress.add_task("download", total=None)
                lines = download_wordlist(on_progress=lambda done, total: progress.update(task, completed=done, total=total))
        except DownloadError as exc:
            self.console.print(f"{mark(False)} Could not download the word list: {exc}")
            self.pause()
            return None
        self.console.print(f"{mark()} Downloaded {lines:,} words to {ENGLISH_WORDS_PATH}")
        return ENGLISH_WORDS_PATH

    def ask_for_file(self) -> Path | None:
        self.console.print(Text("Drag your word list file into this window, then press Enter.", style="bold"))
        self.console.print("You can also type the full path. Leave it empty to go back.", style=MUTED)
        while True:
            raw = Prompt.ask("File", default="", show_default=False, console=self.console).strip().strip("\"'").strip()
            if not raw:
                return None
            path = Path(raw).expanduser()
            if path.is_file():
                return path
            self.console.print(f"{mark(False)} There is no file at {path}. Please try again.")

    def pick_lengths(self) -> tuple[int | None, int | None]:
        choice = self.choose(
            "How long may the names be?",
            [
                ("1", "Any length", f"{MIN_LENGTH}–{MAX_LENGTH} characters"),
                ("2", "Short", "up to 6 characters · rare, most are taken"),
                ("3", "Medium", "up to 10 characters"),
                ("4", "Let me choose", ""),
            ],
            default="1",
        )
        if choice != "4":
            return {"1": (None, None), "2": (None, 6), "3": (None, 10)}[choice]
        while True:
            shortest = IntPrompt.ask("Shortest", default=MIN_LENGTH, console=self.console)
            longest = IntPrompt.ask("Longest", default=MAX_LENGTH, console=self.console)
            if MIN_LENGTH <= shortest <= longest <= MAX_LENGTH:
                return shortest, longest
            self.console.print(f"{mark(False)} Use numbers between {MIN_LENGTH} and {MAX_LENGTH}, shortest first.")

    def pick_affixes(self) -> tuple[str, str]:
        if not Confirm.ask("Add letters before or after every word (glacier → glaciermc)?", default=False, console=self.console):
            return "", ""
        return self.ask_affix("Letters BEFORE each word"), self.ask_affix("Letters AFTER each word")

    def ask_affix(self, label: str) -> str:
        while True:
            value = Prompt.ask(f"  {label} (Enter for none)", default="", show_default=False, console=self.console).strip()
            if _AFFIX_RE.fullmatch(value):
                return value
            self.console.print(f"{mark(False)} Only letters, numbers and _ are allowed in Minecraft names.")

    def after_scan(self, code: int, *, short_length: int | None = None) -> None:
        if code == commands.EXIT_INTERRUPTED:
            self.console.print(
                "[yellow]Paused.[/] Everything found so far is saved. "
                "Choose [bold]Continue my unfinished search[/] in the menu to carry on."
            )
        if short_length is not None and code in (0, commands.EXIT_INTERRUPTED) and self.offer_confirm(short_length):
            return
        available = self.output_dir / "available.txt"
        if available.exists() and Confirm.ask("Open the folder with your results?", default=True, console=self.console):
            self.open_folder(self.output_dir)
        self.pause()

    # -- 3: resume ---------------------------------------------------------------------------

    def resume(self) -> None:
        if self.blocked_by_running_scan():
            return
        jobs = self.unfinished()
        if not jobs:
            self.console.print("There is no unfinished search.")
            self.pause()
            return
        summary = jobs[0]
        if len(jobs) > 1:
            options: list[Option] = [
                (
                    str(index),
                    Path(item.job.wordlist).name,
                    f"{item.done:,} of {item.job.total:,} done ({percent(item.done, item.job.total)}) · last run {humanize_ago(item.job.updated_at)}",
                )
                for index, item in enumerate(jobs[:9], start=1)
            ]
            options.append(("0", "Back", ""))
            choice = self.choose("Which search do you want to continue?", options, default="1")
            if choice == "0":
                return
            summary = jobs[int(choice) - 1]
        wordlist = Path(summary.job.wordlist)
        if not wordlist.is_file():
            self.console.print(f"{mark(False)} The word list {wordlist} is gone, so this search cannot continue.")
            self.pause()
            return
        options_ = CandidateOptions.from_dict(summary.job.options)
        if wordlist_fingerprint(wordlist, options_) != summary.job.id:
            self.console.print(
                "[yellow]That word list changed since the search started, so it will be read again.[/] "
                "Names that were already checked are skipped."
            )
        code = self.run(commands.scan, wordlist=wordlist, simple=True, **options_.scan_arguments())
        self.after_scan(code, short_length=short_name_length(wordlist))

    def offer_confirm(self, max_length: int) -> bool:
        """After a short-name search, offer Mojang's own check right away. True if it was run."""
        if self.is_demo:
            return False
        waiting = self.unconfirmed(max_length)
        if not waiting:
            return False
        self.console.print(
            f"\n[bold]{waiting:,} short names look free.[/] Many short names that look free are really blocked "
            "or locked by Mojang, and only Mojang's own check can tell which ones you can claim."
        )
        if not Confirm.ask("Check them with Mojang now? (needs your login)", default=True, console=self.console):
            return False
        self.console.print()
        self.confirm_best(max_length=max_length)
        return True

    # -- 4: show -----------------------------------------------------------------------------

    def show_found(self) -> None:
        counts = self.counts()
        found = counts.get(Status.AVAILABLE, 0) + counts.get(Status.SOON, 0)
        if not found:
            self.console.print("No free names found yet. Choose [bold]2[/] in the menu to start a search.")
            self.pause()
            return
        label, sort = self.pick_sort()
        with Database(self.db_path) as database:
            shown = list(database.iter_results(statuses={Status.AVAILABLE, Status.SOON}, sort=sort.value, limit=30))
        totals = f"{counts.get(Status.AVAILABLE, 0):,} free, {counts.get(Status.SOON, 0):,} coming soon"
        title = f"Your best finds: {totals}" if sort is commands.SortKey.best else f"Your finds ({label.lower()}): {totals}"
        self.console.print(results_preview(shown, title=title))
        if self.scan_running():
            self.console.print("A search is still running, so this list keeps growing. Choose 4 again to refresh.")
        if found > len(shown):
            first = "best" if sort is commands.SortKey.best else "first"
            self.console.print(f"Showing the {first} {len(shown)}. Choose [bold]5[/] in the menu to save all {found:,} to a file.")
        self.console.print(
            f"[{MUTED}]{symbol(Status.AVAILABLE)} likely free: nobody uses it right now, so try claiming it at minecraft.net. "
            f"{symbol(Status.SOON)} coming soon: on hold after a rename.[/]"
        )
        self.pause()

    # -- 5: save -----------------------------------------------------------------------------

    def save_results(self) -> None:
        choice = self.choose(
            "How do you want to save your results?",
            [
                ("1", "Spreadsheet", "CSV file · opens in Excel"),
                ("2", "Simple list", "TXT file · one name per line"),
                ("0", "Back", ""),
            ],
            default="1",
        )
        if choice == "0":
            return
        fmt = commands.ExportFormat.csv if choice == "1" else commands.ExportFormat.txt
        self.console.print()
        _, sort = self.pick_sort()
        if self.run(commands.export, fmt=fmt, sort=sort, demo=self.is_demo) == 0 and Confirm.ask("Open the folder?", default=True, console=self.console):
            self.open_folder(self.output_dir)
        self.pause()

    # -- 6: confirm --------------------------------------------------------------------------

    def unconfirmed(self, max_length: int | None = None) -> int:
        """How many "likely available" names have not been confirmed with Mojang yet."""
        if not self.db_path.exists():
            return 0
        with Database(self.db_path) as database:
            found = database.iter_results(statuses={Status.AVAILABLE}, unverified_only=True, max_length=max_length, limit=100_000)
            return sum(1 for _ in found)

    def confirm_best(self, *, max_length: int | None = None) -> None:
        if self.is_demo:
            self.console.print("Confirming only works with real results (the demo provider is the default right now).")
            self.pause()
            return
        if not self.db_path.exists():
            self.console.print("There are no results yet. Choose [bold]2[/] to start a search first.")
            self.pause()
            return
        waiting = self.unconfirmed(max_length)
        if not waiting:
            self.console.print("Nothing to confirm: every 'likely available' name has already been checked.")
            self.pause()
            return
        if max_length is None:
            short = self.unconfirmed(max(SHORT_NAME_LENGTHS))
            if 0 < short < waiting:
                choice = self.choose(
                    "Which names should I check?",
                    [
                        ("1", "My best names", f"{waiting:,} waiting"),
                        ("2", "Only short names", f"3 or 4 characters · {short:,} waiting"),
                    ],
                    default="1",
                )
                self.console.print()
                if choice == "2":
                    max_length, waiting = max(SHORT_NAME_LENGTHS), short
        per_minute = commands.verify_per_minute(self.config)
        self.console.print(
            Text.assemble(
                ("Without your login, Mojang only says whether an account owns a name. Free names, names "
                 "on hold after a rename, and blocked or locked names all look the same. Mojang's own check (the one "
                 "minecraft.net uses when you change your name) tells them apart, but it needs your login and "
                 "allows only about ", ""),
                (f"{per_minute:g} names per minute", "bold"),
                (". So your best names are checked first.", ""),
            )
        )  # fmt: skip
        kind = "names" if max_length is None else f"names of up to {max_length} characters"
        self.console.print(f"[{MUTED}]{waiting:,} {kind} are waiting to be confirmed.[/]\n")

        def minutes(count: int) -> str:
            return format_duration(min(count, waiting) / per_minute * 60)

        choice = self.choose(
            f"How many of your best {kind} should I check?",
            [
                ("1", "Best 25", f"about {minutes(25)}"),
                ("2", "Best 100", f"about {minutes(100)}"),
                ("3", "Best 300", f"about {minutes(300)} · fine to leave running"),
                ("4", "Let me choose", ""),
                ("0", "Back", ""),
            ],
            default="2",
        )
        if choice == "0":
            return
        top = {"1": 25, "2": 100, "3": 300}.get(choice) or IntPrompt.ask("How many", default=50, console=self.console)
        self.console.print()
        token = commands.ask_token(self.console, self.config)
        if token is None:
            self.console.print("No token given, so nothing was checked.")
            self.pause()
            return
        self.console.print()
        commands.run_verify(self.app, top=max(1, top), token=token, max_length=max_length)
        confirmed = self.output_dir / "confirmed.txt"
        if confirmed.exists() and Confirm.ask("Open the folder with your results?", default=True, console=self.console):
            self.open_folder(self.output_dir)
        self.pause()

    # -- 7: help -----------------------------------------------------------------------------

    def help(self) -> None:
        free, soon, taken = symbol(Status.AVAILABLE), symbol(Status.SOON), symbol(Status.TAKEN)
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold", no_wrap=True)
        table.add_column()
        rows = [
            ("What it does", "It turns a list of words into possible names and asks Mojang (the makers of Minecraft) "
                             "whether each one is in use. Names nobody uses are saved for you."),
            ("", ""),
            (Text(f"{free} likely free", style="green"), "Nobody uses this name right now. Short real words among these are "
                                                       "often blocked or on hold; option 6 asks Mojang which ones are really free."),
            (Text(f"{soon} coming soon", style="yellow"), "Someone gave this name up recently. Mojang holds old names for 37 days, "
                                                        "and the exact release time is usually unknown. Some short names "
                                                        "stay locked much longer."),
            (Text(f"{taken} taken", style="red"), "Someone owns it."),
            ("", ""),
            ("How long", "About 36,000 names per hour. That is Mojang's speed limit; going faster gets you blocked. "
                         "The whole English dictionary takes about 10 hours, so start it in the evening. Keep the PC plugged in; "
                         "it is kept awake automatically while searching."),
            ("", ""),
            ("Stopping", "Press Ctrl+C at any time. Everything found so far is saved. "
                         "Choose 'Continue my unfinished search' later to pick up where you left off."),
            ("", ""),
            ("Tips", "Most normal words are taken. Long or unusual words, or adding letters before or after "
                     "each word (like 'mc'), give you better chances."),
            ("", ""),
            ("Short names", "'Every short name' in option 2 tries all 3- or 4-character names. Many that look free are "
                            "really blocked or locked, so check them with Mojang afterwards (option 6)."),
        ]  # fmt: skip
        for label, text in rows:
            table.add_row(label, text)
        self.console.print(Panel(table, title=Text(" How it works ", style=f"bold {ACCENT}"), border_style=ACCENT, box=box.ROUNDED, padding=(1, 2)))
        self.pause()


def run_menu(ctx: typer.Context) -> None:
    Menu(ctx).loop()
