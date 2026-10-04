"""Crash-safe output files: ``available.txt``, ``soon.txt`` and ``results.jsonl``.

Every record is a single line written with one ``write`` call and flushed
immediately; discoveries are also ``fsync``-ed right away, the JSONL log in
small batches. If a previous run died mid-line, the torn line is terminated
before anything new is appended, so it can never corrupt the next record.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .models import CheckResult, Confidence, Status
from .rate_limit import Clock
from .timeutil import to_iso


class AppendOnlyFile:
    def __init__(
        self,
        path: Path,
        *,
        fsync_every: int = 1,
        fsync_interval: float = 2.0,
        clock: Clock = time.monotonic,
    ) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        torn = False
        if path.exists() and path.stat().st_size > 0:
            with path.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                torn = handle.read(1) != b"\n"
        self._handle = path.open("ab")
        if torn:
            self._handle.write(b"\n")
        self._fsync_every = max(1, fsync_every)
        self._fsync_interval = fsync_interval
        self._clock = clock
        self._unsynced = 0
        self._last_sync = clock()

    def write_line(self, line: str) -> None:
        data = (line.replace("\r", " ").replace("\n", " ") + "\n").encode("utf-8")
        self._handle.write(data)
        self._handle.flush()
        self._unsynced += 1
        if (
            self._unsynced >= self._fsync_every
            or self._clock() - self._last_sync >= self._fsync_interval
        ):
            self.sync()

    def sync(self) -> None:
        if self._unsynced and not self._handle.closed:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._unsynced = 0
            self._last_sync = self._clock()

    def close(self) -> None:
        if not self._handle.closed:
            self.sync()
            self._handle.close()


def read_names(path: Path) -> set[str]:
    """Lower-cased first token of every line (tolerates a torn last line)."""
    if not path.exists():
        return set()
    names: set[str] = set()
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            token = line.split(maxsplit=1)
            if token and not token[0].startswith("#"):
                names.add(token[0].lower())
    return names


class ResultWriter:
    """Appends discoveries and the result log to the output directory.

    * ``available.txt`` — one AVAILABLE name per line (de-duplicated across runs)
    * ``confirmed.txt`` — AVAILABLE names confirmed by Mojang's own check (``verify``)
    * ``soon.txt``      — ``name<TAB>estimated <ISO time>`` or ``name<TAB>release unknown``
    * ``results.jsonl`` — one JSON object per freshly checked name (an append-only log)
    """

    def __init__(self, directory: Path, *, jsonl: bool = True) -> None:
        self.directory = directory
        self.available_path = directory / "available.txt"
        self.soon_path = directory / "soon.txt"
        self.confirmed_path = directory / "confirmed.txt"
        self.jsonl_path: Path | None = directory / "results.jsonl" if jsonl else None
        self._seen_available = read_names(self.available_path)
        self._seen_soon = read_names(self.soon_path)
        self._seen_confirmed = read_names(self.confirmed_path)
        self._confirmed: AppendOnlyFile | None = None
        self._available: AppendOnlyFile | None = None
        self._soon: AppendOnlyFile | None = None
        self._jsonl: AppendOnlyFile | None = None
        self.written_available = 0
        self.written_soon = 0

    def record(self, result: CheckResult, *, fresh: bool = True) -> None:
        if fresh and self.jsonl_path is not None:
            if self._jsonl is None:
                self._jsonl = AppendOnlyFile(self.jsonl_path, fsync_every=200)
            self._jsonl.write_line(
                json.dumps(result.to_json(), ensure_ascii=False, separators=(",", ":"))
            )
        key = result.key
        if result.status is Status.AVAILABLE and result.confidence is Confidence.CONFIRMED and key not in self._seen_confirmed:
            if self._confirmed is None:
                self._confirmed = AppendOnlyFile(self.confirmed_path)
            self._confirmed.write_line(result.name)
            self._seen_confirmed.add(key)
        if result.status is Status.AVAILABLE and key not in self._seen_available:
            if self._available is None:
                self._available = AppendOnlyFile(self.available_path)
            self._available.write_line(result.name)
            self._seen_available.add(key)
            self.written_available += 1
        elif result.status is Status.SOON and key not in self._seen_soon:
            if self._soon is None:
                self._soon = AppendOnlyFile(self.soon_path)
            when = (
                f"estimated {to_iso(result.available_at)}"
                if result.available_at is not None
                else "release unknown"
            )
            self._soon.write_line(f"{result.name}\t{when}")
            self._seen_soon.add(key)
            self.written_soon += 1

    def flush(self) -> None:
        for handle in (self._available, self._soon, self._confirmed, self._jsonl):
            if handle is not None:
                handle.sync()

    def close(self) -> None:
        for handle in (self._available, self._soon, self._confirmed, self._jsonl):
            if handle is not None:
                handle.close()

    def __enter__(self) -> ResultWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
