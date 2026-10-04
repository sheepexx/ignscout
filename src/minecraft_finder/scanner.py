"""The asynchronous scan pipeline.

::

    word list ─► SQLite staging (validated, filtered, de-duplicated)
                     │
                 producer ── cache lookup ──► cached result ──────────────┐
                     │                                                    │
                 asyncio.Queue                                            │
                     │                                                    ▼
                 N workers ─► provider.check_many() ─► enrichment ─► result handler
                              (rate limiter, retries,                 (SQLite, output files,
                               backoff, 429 handling)                  dashboard state)

Progress is checkpointed at least once per second: results and their ``done``
flags are committed in the same transaction. On cancellation (Ctrl+C) the
workers are cancelled, completed results are flushed, and :meth:`Scanner.run`
returns normally with ``interrupted=True``. Items whose request was in flight
stay pending and are picked up by the next run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from .availability import combine_with_enrichment
from .database import Database, PendingItem
from .models import CheckResult, Confidence, Status
from .namefilter import flag_if_offensive, is_offensive, skipped_result
from .output import ResultWriter
from .providers.base import Provider
from .ranking import quality_score
from .wordlist import is_dictionary_word

logger = logging.getLogger(__name__)


@dataclass
class ScanCounters:
    checked: int = 0
    available: int = 0
    confirmed: int = 0
    soon: int = 0
    taken: int = 0
    blocked: int = 0
    unknown: int = 0
    errors: int = 0
    cache_hits: int = 0

    def add(self, result: CheckResult) -> None:
        self.checked += 1
        if result.from_cache:
            self.cache_hits += 1
        match result.status:
            case Status.AVAILABLE:
                self.available += 1
                if result.confidence is Confidence.CONFIRMED:
                    self.confirmed += 1
            case Status.SOON:
                self.soon += 1
            case Status.TAKEN:
                self.taken += 1
            case Status.BLOCKED:
                self.blocked += 1
            case Status.UNKNOWN:
                self.unknown += 1
            case Status.ERROR:
                self.errors += 1

    @classmethod
    def from_counts(cls, counts: dict[Status, int], confirmed: int = 0) -> ScanCounters:
        counters = cls(
            available=counts.get(Status.AVAILABLE, 0),
            confirmed=confirmed,
            soon=counts.get(Status.SOON, 0),
            taken=counts.get(Status.TAKEN, 0),
            blocked=counts.get(Status.BLOCKED, 0),
            unknown=counts.get(Status.UNKNOWN, 0),
            errors=counts.get(Status.ERROR, 0),
        )
        counters.checked = sum(counts.values())
        return counters


@dataclass
class ScanState:
    """Live, UI-agnostic view of a running scan (read by the dashboard)."""

    total: int
    done_at_start: int = 0
    counters: ScanCounters = field(default_factory=ScanCounters)  # whole job (resumed counts included)
    session: ScanCounters = field(default_factory=ScanCounters)  # this run only
    latest: deque[CheckResult] = field(default_factory=lambda: deque(maxlen=8))
    discoveries: list[CheckResult] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    phase: str = "Starting"
    max_discoveries: int = 5_000

    @property
    def session_processed(self) -> int:
        return self.session.checked

    @property
    def processed(self) -> int:
        return self.done_at_start + self.session.checked

    def record(self, result: CheckResult) -> None:
        self.counters.add(result)
        self.session.add(result)
        if result.status.is_discovery:
            self.latest.appendleft(result)
            if len(self.discoveries) < self.max_discoveries:
                self.discoveries.append(result)


@dataclass(frozen=True)
class ScanOptions:
    workers: int = 4
    cache_ttl: timedelta = timedelta(hours=24)
    force: bool = False
    page_size: int = 500
    flush_every: int = 200
    flush_interval: float = 1.0
    filter_offensive: bool = True


@dataclass(frozen=True)
class ScanOutcome:
    completed: bool
    interrupted: bool
    total: int
    done: int
    session_processed: int
    session_errors: int
    elapsed: float

    @property
    def remaining(self) -> int:
        return max(self.total - self.done, 0)


class Scanner:
    def __init__(
        self,
        *,
        db: Database,
        job_id: str,
        provider: Provider,
        state: ScanState,
        options: ScanOptions,
        writer: ResultWriter | None = None,
        enrichers: Sequence[Provider] = (),
        on_result: Callable[[CheckResult], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.job_id = job_id
        self.provider = provider
        self.state = state
        self.options = options
        self.writer = writer
        self.enrichers = list(enrichers)
        self.on_result = on_result
        self._clock = clock
        self._pending_results: list[CheckResult] = []
        self._pending_done: list[str] = []

    async def run(self) -> ScanOutcome:
        started = self._clock()
        self.state.phase = "Scanning"
        batch = max(1, self.provider.max_batch_size)
        queue: asyncio.Queue[PendingItem | None] = asyncio.Queue(
            maxsize=max(100, self.options.workers * batch * 4)
        )
        tasks = [asyncio.create_task(self._produce(queue), name="mcf-producer")]
        tasks += [
            asyncio.create_task(self._work(queue), name=f"mcf-worker-{index}")
            for index in range(self.options.workers)
        ]
        flusher = asyncio.create_task(self._flush_loop(), name="mcf-flusher")

        interrupted = False
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            interrupted = True
            self.state.phase = "Stopping"
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()  # we handle the cancellation: shut down cleanly instead
            logger.info("scan interrupted — stopping workers and saving progress")
        finally:
            for task in (*tasks, flusher):
                task.cancel()
            await asyncio.gather(*tasks, flusher, return_exceptions=True)
            self._flush()

        total, done = self.db.job_progress(self.job_id)
        completed = not interrupted and done >= total
        if completed:
            self.db.finish_job(self.job_id)
        self.state.phase = "Stopped" if interrupted else "Done"
        outcome = ScanOutcome(
            completed=completed,
            interrupted=interrupted,
            total=total,
            done=done,
            session_processed=self.state.session.checked,
            session_errors=self.state.session.errors,
            elapsed=self._clock() - started,
        )
        logger.info("scan finished: %s", outcome)
        return outcome

    # -- pipeline stages ---------------------------------------------------------------------

    async def _produce(self, queue: asyncio.Queue[PendingItem | None]) -> None:
        after = -1
        while True:
            items = self.db.pending_items(self.job_id, after_seq=after, limit=self.options.page_size)
            if not items:
                break
            after = items[-1].seq
            cached = (
                {}
                if self.options.force
                else self.db.get_fresh(
                    [item.username for item in items],
                    max_age=self.options.cache_ttl,
                    provider=self.provider.name,
                )
            )
            for item in items:
                hit = cached.get(item.username)
                if hit is not None:
                    hit.from_cache = True
                    self._handle(hit, item)
                elif self.options.filter_offensive and is_offensive(item.username, item.source_word):
                    # Mojang would refuse the name anyway: don't spend a request on it.
                    self._handle(skipped_result(item.username, self.provider.name, item.source_word), item)
                else:
                    await queue.put(item)
            await asyncio.sleep(0)  # keep the UI responsive during long runs of cache hits
        for _ in range(self.options.workers):
            await queue.put(None)

    async def _work(self, queue: asyncio.Queue[PendingItem | None]) -> None:
        batch_size = max(1, self.provider.max_batch_size)
        while True:
            item = await queue.get()
            if item is None:
                return
            batch = [item]
            stop = False
            while len(batch) < batch_size:
                try:
                    extra = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if extra is None:
                    stop = True
                    break
                batch.append(extra)
            await self._process(batch)
            if stop:
                return

    async def _process(self, batch: list[PendingItem]) -> None:
        names = [item.username for item in batch]
        try:
            results = await self.provider.check_many(names)
            if len(results) != len(names):
                raise RuntimeError(f"provider returned {len(results)} results for {len(names)} names")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a provider bug must not kill the whole scan
            logger.exception("provider failure for batch starting with %s", names[0])
            results = [
                CheckResult(username=name, status=Status.ERROR, provider=self.provider.name, last_error=f"internal error: {exc}")
                for name in names
            ]
        for item, result in zip(batch, results, strict=True):
            result = await self._enrich(result)
            self._handle(result, item)

    async def _enrich(self, result: CheckResult) -> CheckResult:
        if result.status is not Status.AVAILABLE or result.confidence is Confidence.CONFIRMED:
            return result
        for enricher in self.enrichers:
            if not enricher.enabled:
                continue
            try:
                extra = await enricher.check(result.username)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("enrichment by %s failed for %s", enricher.name, result.username)
                continue
            result = combine_with_enrichment(result, extra)
            if result.status is not Status.AVAILABLE:
                break
        return result

    def _handle(self, result: CheckResult, item: PendingItem) -> None:
        if result.source_word is None:
            result.source_word = item.source_word
        if self.options.filter_offensive:
            flagged = flag_if_offensive(result, item.source_word)
            if flagged is not result:
                flagged.from_cache = False  # store the reclassification
                result = flagged
        if result.quality_score is None:
            result.quality_score = quality_score(
                result.username, dictionary_word=is_dictionary_word(result.username, item.source_word)
            )
        self.state.record(result)
        if not result.from_cache:
            self._pending_results.append(result)
        if result.status is not Status.ERROR:
            self._pending_done.append(item.username)  # ERROR items stay pending -> retried next run
        if self.writer is not None:
            self.writer.record(result, fresh=not result.from_cache)
        if self.on_result is not None:
            self.on_result(result)
        if len(self._pending_results) + len(self._pending_done) >= self.options.flush_every:
            self._flush()

    # -- checkpointing -----------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.options.flush_interval)
            self._flush()

    def _flush(self) -> None:
        if not self._pending_results and not self._pending_done:
            return
        try:
            self.db.upsert_results(self._pending_results)
            self.db.mark_done(self.job_id, self._pending_done)
            self.db.touch_job(self.job_id)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if self.writer is not None:
            self.writer.flush()
        self._pending_results.clear()
        self._pending_done.clear()
