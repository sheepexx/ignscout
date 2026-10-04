"""SQLite persistence: the result cache, scan jobs and resumable scan progress.

Tables
------
``results``     one row per username (lower-case key) with the latest known status.
``scan_jobs``   one row per distinct scan (word list + options fingerprint).
``scan_items``  the de-duplicated candidates of a job and whether each is done.

Progress is resumable because an item is only marked ``done`` in the same
transaction that stores its result. Items whose check failed (``ERROR``) stay
pending, so re-running the same command retries them.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import CheckResult, Confidence, Status
from .timeutil import from_iso, to_iso, utcnow
from .wordlist import Candidate

SCHEMA_VERSION = 1
_CHUNK = 500
_STAGE_COMMIT_EVERY = 100_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    username      TEXT PRIMARY KEY,      -- lower-case key; names are case-insensitive
    display_name  TEXT NOT NULL,         -- canonical spelling when known
    status        TEXT NOT NULL,
    confidence    TEXT NOT NULL DEFAULT 'none',
    uuid          TEXT,
    provider      TEXT NOT NULL,
    checked_at    TEXT NOT NULL,         -- ISO-8601 UTC
    available_at  TEXT,                  -- ISO-8601 UTC; SOON results with an estimate only
    detail        TEXT,
    last_error    TEXT,
    quality_score REAL,
    source_word   TEXT
);
CREATE INDEX IF NOT EXISTS idx_results_status ON results(status);
CREATE INDEX IF NOT EXISTS idx_results_checked ON results(checked_at);
CREATE TABLE IF NOT EXISTS scan_jobs (
    id          TEXT PRIMARY KEY,
    wordlist    TEXT NOT NULL,
    options     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    staged      INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    read_stats  TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS scan_items (
    job_id      TEXT NOT NULL REFERENCES scan_jobs(id) ON DELETE CASCADE,
    username    TEXT NOT NULL,
    source_word TEXT,
    seq         INTEGER NOT NULL,
    done        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (job_id, username)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_items_pending ON scan_items(job_id, done, seq);
"""

_RESULT_COLUMNS = (
    "username",
    "display_name",
    "status",
    "confidence",
    "uuid",
    "provider",
    "checked_at",
    "available_at",
    "detail",
    "last_error",
    "quality_score",
    "source_word",
)
_INSERT = (
    f"INSERT INTO results ({', '.join(_RESULT_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_RESULT_COLUMNS))})"
)
_UPSERT = (
    _INSERT
    + """
ON CONFLICT(username) DO UPDATE SET
    display_name  = excluded.display_name,
    status        = excluded.status,
    confidence    = excluded.confidence,
    uuid          = excluded.uuid,
    provider      = excluded.provider,
    checked_at    = excluded.checked_at,
    available_at  = excluded.available_at,
    detail        = excluded.detail,
    last_error    = excluded.last_error,
    quality_score = COALESCE(excluded.quality_score, results.quality_score),
    source_word   = COALESCE(excluded.source_word, results.source_word)
"""
)
# A failed check must never overwrite an earlier real answer; only the error is recorded.
_UPSERT_ERROR = (
    _INSERT
    + """
ON CONFLICT(username) DO UPDATE SET
    last_error    = excluded.last_error,
    checked_at    = CASE WHEN results.status = 'ERROR' THEN excluded.checked_at ELSE results.checked_at END,
    quality_score = COALESCE(excluded.quality_score, results.quality_score),
    source_word   = COALESCE(excluded.source_word, results.source_word)
"""
)

SORT_ORDERS = {
    "best": "(status = 'AVAILABLE' AND confidence = 'confirmed') DESC, quality_score IS NULL, quality_score DESC, length(username), username",
    "quality": "quality_score IS NULL, quality_score DESC, length(username), username",
    "name": "username",
    "length": "length(username), username",
    "checked": "checked_at DESC, username",
    "release": "available_at IS NULL, available_at, username",
}


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    wordlist: str
    options: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    staged: bool
    total: int
    read_stats: dict[str, int]
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class JobSummary:
    job: JobRecord
    done: int


@dataclass(frozen=True, slots=True)
class PendingItem:
    seq: int
    username: str
    source_word: str | None


def _chunks(items: Sequence[str], size: int = _CHUNK) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _placeholders(count: int) -> str:
    return ", ".join("?" * count)


def _result_params(result: CheckResult) -> tuple[Any, ...]:
    return (
        result.key,
        result.name,
        result.status.value,
        result.confidence.value,
        result.uuid,
        result.provider,
        to_iso(result.checked_at),
        to_iso(result.available_at),
        result.detail,
        result.last_error,
        result.quality_score,
        result.source_word,
    )


def _row_to_result(row: sqlite3.Row) -> CheckResult:
    return CheckResult(
        username=row["username"],
        display_name=row["display_name"],
        status=Status(row["status"]),
        confidence=Confidence(row["confidence"]),
        uuid=row["uuid"],
        provider=row["provider"],
        checked_at=from_iso(row["checked_at"]) or utcnow(),
        available_at=from_iso(row["available_at"]),
        detail=row["detail"],
        last_error=row["last_error"],
        quality_score=row["quality_score"],
        source_word=row["source_word"],
    )


def _row_to_job(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        id=row["id"],
        wordlist=row["wordlist"],
        options=json.loads(row["options"]),
        created_at=from_iso(row["created_at"]) or utcnow(),
        updated_at=from_iso(row["updated_at"]) or utcnow(),
        staged=bool(row["staged"]),
        total=row["total"],
        read_stats=json.loads(row["read_stats"]) if row["read_stats"] else {},
        finished_at=from_iso(row["finished_at"]),
    )


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    # -- lifecycle ---------------------------------------------------------------------------

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        try:
            self._conn.commit()
        finally:
            self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- results / cache ---------------------------------------------------------------------

    def upsert_results(self, results: Iterable[CheckResult]) -> None:
        normal: list[tuple[Any, ...]] = []
        errors: list[tuple[Any, ...]] = []
        for result in results:
            (errors if result.status is Status.ERROR else normal).append(_result_params(result))
        if normal:
            self._conn.executemany(_UPSERT, normal)
        if errors:
            self._conn.executemany(_UPSERT_ERROR, errors)

    def upsert_result(self, result: CheckResult) -> None:
        self.upsert_results([result])

    def get_result(self, username: str) -> CheckResult | None:
        row = self._conn.execute(
            "SELECT * FROM results WHERE username = ?", (username.lower(),)
        ).fetchone()
        return _row_to_result(row) if row else None

    def get_fresh(
        self,
        usernames: Sequence[str],
        *,
        max_age: timedelta,
        provider: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, CheckResult]:
        """Cached definitive results younger than ``max_age``, keyed by lower-case username.

        ERROR and UNKNOWN results are never served from the cache, results from a
        different primary provider are ignored, and SOON results whose estimated
        release time has passed are treated as stale.
        """
        now = now or utcnow()
        if max_age <= timedelta(0) or not usernames:
            return {}
        cutoff = to_iso(now - max_age)
        keys = list(dict.fromkeys(name.lower() for name in usernames))
        found: dict[str, CheckResult] = {}
        for chunk in _chunks(keys):
            rows = self._conn.execute(
                f"SELECT * FROM results WHERE username IN ({_placeholders(len(chunk))}) "
                "AND checked_at >= ? AND status IN ('AVAILABLE', 'SOON', 'TAKEN', 'BLOCKED')",
                (*chunk, cutoff),
            )
            for row in rows:
                result = _row_to_result(row)
                if provider and result.provider.split("+")[0] != provider:
                    continue
                if (
                    result.status is Status.SOON
                    and result.available_at is not None
                    and result.available_at <= now
                ):
                    continue
                found[result.key] = result
        return found

    def count_uncached_pending(
        self, job_id: str, *, max_age: timedelta, provider: str, now: datetime | None = None
    ) -> int:
        """Pending items of a job that the cache cannot answer (i.e. that need a request).

        Mirrors the rules of :meth:`get_fresh` in SQL so huge jobs are counted quickly.
        """
        if max_age <= timedelta(0):
            return self._conn.execute(
                "SELECT COUNT(*) FROM scan_items WHERE job_id = ? AND done = 0", (job_id,)
            ).fetchone()[0]
        now = now or utcnow()
        return self._conn.execute(
            "SELECT COUNT(*) FROM scan_items i WHERE i.job_id = ? AND i.done = 0 AND NOT EXISTS ("
            " SELECT 1 FROM results r WHERE r.username = i.username AND r.checked_at >= ?"
            " AND r.status IN ('AVAILABLE', 'SOON', 'TAKEN', 'BLOCKED')"
            " AND (r.provider = ? OR r.provider LIKE ?)"
            " AND NOT (r.status = 'SOON' AND r.available_at IS NOT NULL AND r.available_at <= ?))",
            (job_id, to_iso(now - max_age), provider, f"{provider}+%", to_iso(now)),
        ).fetchone()[0]

    def discovery_names(self) -> list[tuple[str, str | None]]:
        """``(username, source_word)`` of every AVAILABLE or SOON result."""
        rows = self._conn.execute("SELECT username, source_word FROM results WHERE status IN ('AVAILABLE', 'SOON')")
        return [(row[0], row[1]) for row in rows]

    def mark_probably_blocked(self, usernames: Sequence[str], detail: str) -> int:
        """Reclassify AVAILABLE/SOON results as BLOCKED (unverified). Returns how many changed."""
        cursor = self._conn.executemany(
            "UPDATE results SET status = 'BLOCKED', confidence = 'unverified', available_at = NULL, detail = ? "
            "WHERE username = ? AND status IN ('AVAILABLE', 'SOON')",
            [(detail, name.lower()) for name in usernames],
        )
        self._conn.commit()
        return cursor.rowcount

    # -- scan jobs ---------------------------------------------------------------------------

    def get_job(self, job_id: str) -> JobRecord | None:
        row = self._conn.execute("SELECT * FROM scan_jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def create_job(self, job_id: str, wordlist: str, options: dict[str, Any]) -> JobRecord:
        now = to_iso(utcnow())
        self._conn.execute(
            "INSERT OR IGNORE INTO scan_jobs (id, wordlist, options, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_id, wordlist, json.dumps(options, sort_keys=True), now, now),
        )
        self._conn.commit()
        job = self.get_job(job_id)
        assert job is not None
        return job

    def stage_items(self, job_id: str, candidates: Iterable[Candidate]) -> int:
        """Insert candidates (duplicates are ignored on disk). Returns the unique total."""
        sql = "INSERT OR IGNORE INTO scan_items (job_id, username, source_word, seq) VALUES (?, ?, ?, ?)"
        buffer: list[tuple[str, str, str, int]] = []
        since_commit = 0
        for candidate in candidates:
            buffer.append((job_id, candidate.username.lower(), candidate.source_word, candidate.line_no))
            if len(buffer) >= 5_000:
                self._conn.executemany(sql, buffer)
                since_commit += len(buffer)
                buffer.clear()
                if since_commit >= _STAGE_COMMIT_EVERY:
                    self._conn.commit()
                    since_commit = 0
        if buffer:
            self._conn.executemany(sql, buffer)
        self._conn.commit()
        return self._conn.execute(
            "SELECT COUNT(*) FROM scan_items WHERE job_id = ?", (job_id,)
        ).fetchone()[0]

    def mark_staged(self, job_id: str, total: int, read_stats: dict[str, int]) -> None:
        self._conn.execute(
            "UPDATE scan_jobs SET staged = 1, total = ?, read_stats = ?, updated_at = ? WHERE id = ?",
            (total, json.dumps(read_stats), to_iso(utcnow()), job_id),
        )
        self._conn.commit()

    def reset_job(self, job_id: str) -> None:
        self._conn.execute("UPDATE scan_items SET done = 0 WHERE job_id = ?", (job_id,))
        self._conn.execute(
            "UPDATE scan_jobs SET finished_at = NULL, updated_at = ? WHERE id = ?",
            (to_iso(utcnow()), job_id),
        )
        self._conn.commit()

    def job_progress(self, job_id: str) -> tuple[int, int]:
        """``(total, done)`` for a job."""
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(done), 0) FROM scan_items WHERE job_id = ?", (job_id,)
        ).fetchone()
        return int(row[0]), int(row[1])

    def pending_items(self, job_id: str, *, after_seq: int, limit: int) -> list[PendingItem]:
        rows = self._conn.execute(
            "SELECT seq, username, source_word FROM scan_items "
            "WHERE job_id = ? AND done = 0 AND seq > ? ORDER BY seq LIMIT ?",
            (job_id, after_seq, limit),
        )
        return [PendingItem(row["seq"], row["username"], row["source_word"]) for row in rows]

    def mark_done(self, job_id: str, usernames: Iterable[str]) -> None:
        self._conn.executemany(
            "UPDATE scan_items SET done = 1 WHERE job_id = ? AND username = ?",
            [(job_id, name.lower()) for name in usernames],
        )

    def touch_job(self, job_id: str) -> None:
        self._conn.execute(
            "UPDATE scan_jobs SET updated_at = ? WHERE id = ?", (to_iso(utcnow()), job_id)
        )

    def finish_job(self, job_id: str) -> None:
        now = to_iso(utcnow())
        self._conn.execute(
            "UPDATE scan_jobs SET finished_at = ?, updated_at = ? WHERE id = ?", (now, now, job_id)
        )
        self._conn.commit()

    def job_status_counts(self, job_id: str) -> tuple[dict[Status, int], int]:
        """Status counts of a job's finished items, plus the number of confirmed AVAILABLE ones."""
        rows = self._conn.execute(
            "SELECT r.status AS status, COUNT(*) AS n, "
            "SUM(CASE WHEN r.confidence = 'confirmed' THEN 1 ELSE 0 END) AS confirmed "
            "FROM scan_items i JOIN results r ON r.username = i.username "
            "WHERE i.job_id = ? AND i.done = 1 GROUP BY r.status",
            (job_id,),
        )
        counts: dict[Status, int] = {}
        confirmed = 0
        for row in rows:
            status = Status(row["status"])
            counts[status] = row["n"]
            if status is Status.AVAILABLE:
                confirmed = row["confirmed"] or 0
        return counts, confirmed

    def list_jobs(self) -> list[JobSummary]:
        rows = self._conn.execute(
            "SELECT j.*, (SELECT COUNT(*) FROM scan_items i WHERE i.job_id = j.id AND i.done = 1) "
            "AS done_count FROM scan_jobs j ORDER BY j.updated_at DESC"
        )
        return [JobSummary(_row_to_job(row), row["done_count"]) for row in rows]

    def delete_job(self, job_id: str) -> None:
        self._conn.execute("DELETE FROM scan_items WHERE job_id = ?", (job_id,))
        self._conn.execute("DELETE FROM scan_jobs WHERE id = ?", (job_id,))
        self._conn.commit()

    def count_jobs(self, *, finished_only: bool) -> int:
        where = " WHERE finished_at IS NOT NULL" if finished_only else ""
        return self._conn.execute(f"SELECT COUNT(*) FROM scan_jobs{where}").fetchone()[0]

    def delete_jobs(self, *, finished_only: bool = True) -> int:
        where = " WHERE finished_at IS NOT NULL" if finished_only else ""
        ids = [row[0] for row in self._conn.execute(f"SELECT id FROM scan_jobs{where}")]
        for chunk in _chunks(ids):
            marks = _placeholders(len(chunk))
            self._conn.execute(f"DELETE FROM scan_items WHERE job_id IN ({marks})", tuple(chunk))
            self._conn.execute(f"DELETE FROM scan_jobs WHERE id IN ({marks})", tuple(chunk))
        self._conn.commit()
        return len(ids)

    # -- statistics / export / maintenance ---------------------------------------------------

    def status_counts(self) -> dict[Status, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) FROM results GROUP BY status")
        return {Status(status): count for status, count in rows}

    def confirmed_available(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM results WHERE status = 'AVAILABLE' AND confidence = 'confirmed'"
        ).fetchone()[0]

    def check_time_bounds(self) -> tuple[datetime | None, datetime | None]:
        row = self._conn.execute("SELECT MIN(checked_at), MAX(checked_at) FROM results").fetchone()
        return from_iso(row[0]), from_iso(row[1])

    def iter_results(
        self,
        *,
        statuses: Collection[Status] | None = None,
        sort: str = "quality",
        limit: int | None = None,
        min_score: float | None = None,
        max_length: int | None = None,
        confirmed_only: bool = False,
        unverified_only: bool = False,
    ) -> Iterator[CheckResult]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            clauses.append(f"status IN ({_placeholders(len(statuses))})")
            params.extend(status.value for status in statuses)
        if min_score is not None:
            clauses.append("quality_score >= ?")
            params.append(min_score)
        if max_length is not None:
            clauses.append("length(username) <= ?")
            params.append(max_length)
        if confirmed_only:
            clauses.append("confidence IN ('confirmed', 'estimated')")
        if unverified_only:
            clauses.append("confidence = 'unverified'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        order = SORT_ORDERS.get(sort, SORT_ORDERS["quality"])
        sql = f"SELECT * FROM results{where} ORDER BY {order}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        for row in self._conn.execute(sql, params):
            yield _row_to_result(row)

    def _result_filter(
        self, statuses: Collection[Status] | None, checked_before: datetime | None
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            clauses.append(f"status IN ({_placeholders(len(statuses))})")
            params.extend(status.value for status in statuses)
        if checked_before is not None:
            clauses.append("checked_at < ?")
            params.append(to_iso(checked_before))
        return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), params

    def count_results(
        self, *, statuses: Collection[Status] | None = None, checked_before: datetime | None = None
    ) -> int:
        where, params = self._result_filter(statuses, checked_before)
        return self._conn.execute(f"SELECT COUNT(*) FROM results{where}", params).fetchone()[0]

    def delete_results(
        self, *, statuses: Collection[Status] | None = None, checked_before: datetime | None = None
    ) -> int:
        if not statuses and checked_before is None:
            raise ValueError("refusing to delete every result without a filter")
        where, params = self._result_filter(statuses, checked_before)
        cursor = self._conn.execute(f"DELETE FROM results{where}", params)
        self._conn.commit()
        return cursor.rowcount

    def vacuum(self) -> None:
        self._conn.commit()
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._conn.execute("VACUUM")

    def total_results(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]

    def schema_version(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row[0]) if row else 0
