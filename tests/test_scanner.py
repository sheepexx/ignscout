"""Scan pipeline: completion, cache reuse, retries of errors, and cancellation/resume."""

import asyncio
import json
import random
from datetime import timedelta

import httpx

from minecraft_finder.config import AppConfig
from minecraft_finder.database import Database
from minecraft_finder.models import CheckResult, Confidence, Status
from minecraft_finder.output import ResultWriter
from minecraft_finder.providers.base import Provider, ProviderInfo, ProviderRole
from minecraft_finder.providers.minecraft import MinecraftProvider
from minecraft_finder.scanner import ScanOptions, Scanner, ScanState
from minecraft_finder.timeutil import utcnow
from minecraft_finder.wordlist import CandidateOptions, iter_candidates, wordlist_fingerprint

from .conftest import mock_client

WORDS = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet",
    "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango",
]  # fmt: skip


class FakeProvider(Provider):
    """Names starting with 'a', 'e', 'i' or 'o' are available; everything else is taken."""

    name = "minecraft"
    display_name = "Fake"
    role = ProviderRole.PRIMARY
    max_batch_size = 3

    def __init__(self, *, fail=(), hang_after=None, explode=False):
        super().__init__()
        self.calls: list[str] = []
        self.batches = 0
        self.fail = set(fail)
        self.hang_after = hang_after
        self.explode = explode

    @classmethod
    def info(cls, config):
        return ProviderInfo(cls.name, cls.display_name, cls.role, "fake", (), "none")

    async def check(self, username):
        return (await self.check_many([username]))[0]

    async def check_many(self, names):
        self.batches += 1
        if self.hang_after is not None and self.batches > self.hang_after:
            await asyncio.Event().wait()  # an in-flight request that never finishes
        if self.explode:
            raise RuntimeError("provider bug")
        self.calls.extend(names)
        await asyncio.sleep(0)
        results = []
        for name in names:
            if name in self.fail:
                results.append(self._error(name, "boom"))
            elif name[0] in "aeio":
                results.append(self._result(name, Status.AVAILABLE, confidence=Confidence.UNVERIFIED))
            else:
                results.append(self._result(name, Status.TAKEN, confidence=Confidence.CONFIRMED))
        return results


def staged(tmp_path, words=WORDS):
    wordlist = tmp_path / "words.txt"
    wordlist.write_text("\n".join(words) + "\n", encoding="utf-8")
    options = CandidateOptions()
    job_id = wordlist_fingerprint(wordlist, options)
    db = Database(tmp_path / "results.db")
    db.create_job(job_id, str(wordlist), options.as_dict())
    total = db.stage_items(job_id, iter_candidates(wordlist, options))
    db.mark_staged(job_id, total, {})
    return db, job_id


def make_scanner(db, job_id, provider, *, force=False, writer=None, enrichers=(), workers=2):
    total, done = db.job_progress(job_id)
    state = ScanState(total=total, done_at_start=done)
    options = ScanOptions(workers=workers, force=force, flush_interval=0.05, cache_ttl=timedelta(hours=24))
    return Scanner(db=db, job_id=job_id, provider=provider, state=state, options=options, writer=writer, enrichers=enrichers)


def test_scan_completes_and_persists(tmp_path):
    db, job_id = staged(tmp_path)
    provider = FakeProvider()
    outcome = asyncio.run(make_scanner(db, job_id, provider).run())
    assert outcome.completed and not outcome.interrupted
    assert sorted(provider.calls) == sorted(WORDS)
    assert db.job_progress(job_id) == (20, 20)
    assert db.get_job(job_id).finished_at is not None
    assert db.get_result("alpha").status is Status.AVAILABLE
    assert db.get_result("bravo").status is Status.TAKEN
    assert db.get_result("alpha").quality_score is not None
    db.close()


def test_repeated_scan_is_served_from_cache(tmp_path):
    db, job_id = staged(tmp_path)
    asyncio.run(make_scanner(db, job_id, FakeProvider()).run())
    db.reset_job(job_id)
    second = FakeProvider()
    scanner = make_scanner(db, job_id, second)
    outcome = asyncio.run(scanner.run())
    assert outcome.completed
    assert second.calls == []  # not a single request
    assert scanner.state.session.cache_hits == 20
    db.close()


def test_force_ignores_cache(tmp_path):
    db, job_id = staged(tmp_path)
    asyncio.run(make_scanner(db, job_id, FakeProvider()).run())
    db.reset_job(job_id)
    forced = FakeProvider()
    asyncio.run(make_scanner(db, job_id, forced, force=True).run())
    assert sorted(forced.calls) == sorted(WORDS)
    db.close()


def test_errors_stay_pending_and_are_retried_next_run(tmp_path):
    db, job_id = staged(tmp_path)
    outcome = asyncio.run(make_scanner(db, job_id, FakeProvider(fail={"bravo", "kilo"})).run())
    assert not outcome.completed
    assert outcome.remaining == 2
    assert outcome.session_errors == 2
    retry = FakeProvider()
    outcome = asyncio.run(make_scanner(db, job_id, retry).run())
    assert sorted(retry.calls) == ["bravo", "kilo"]
    assert outcome.completed
    db.close()


def test_cancellation_saves_progress_and_resume_skips_finished_work(tmp_path):
    db, job_id = staged(tmp_path, [f"word{i:03d}" for i in range(30)])
    first = FakeProvider(hang_after=3)  # 3 batches x 3 names finish, then requests hang

    async def interrupt():
        scanner = make_scanner(db, job_id, first)
        task = asyncio.create_task(scanner.run())
        while first.batches < 5:  # both workers are now stuck in a request
            await asyncio.sleep(0.01)
        task.cancel()  # what Ctrl+C does to the main task
        return await task

    outcome = asyncio.run(interrupt())
    assert outcome.interrupted and not outcome.completed
    assert outcome.done == 9
    assert db.job_progress(job_id) == (30, 9)

    second = FakeProvider()
    outcome = asyncio.run(make_scanner(db, job_id, second).run())
    assert outcome.completed
    assert len(second.calls) == 21
    assert not set(second.calls) & set(first.calls)  # finished names were not requested again
    db.close()


def test_case_insensitive_deduplication(tmp_path):
    db, job_id = staged(tmp_path, ["Glacier", "glacier", "GLACIER", "lantern"])
    provider = FakeProvider()
    asyncio.run(make_scanner(db, job_id, provider).run())
    assert sorted(provider.calls) == ["glacier", "lantern"]
    db.close()


def test_writer_receives_discoveries(tmp_path):
    db, job_id = staged(tmp_path)
    writer = ResultWriter(tmp_path / "out")
    asyncio.run(make_scanner(db, job_id, FakeProvider(), writer=writer).run())
    writer.close()
    available = (tmp_path / "out" / "available.txt").read_text(encoding="utf-8").split()
    assert sorted(available) == ["alpha", "echo", "india", "oscar"]
    lines = (tmp_path / "out" / "results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 20
    assert {json.loads(line)["status"] for line in lines} == {"available", "taken"}
    db.close()


def test_provider_exception_becomes_error_results(tmp_path):
    db, job_id = staged(tmp_path, ["alpha", "bravo"])
    outcome = asyncio.run(make_scanner(db, job_id, FakeProvider(explode=True)).run())
    assert outcome.session_errors == 2
    assert db.job_progress(job_id) == (2, 0)  # still pending for the next run
    db.close()


def test_enrichment_can_turn_available_into_soon(tmp_path):
    class Enricher(FakeProvider):
        name = "namemc"

        async def check(self, username):
            return CheckResult(username, Status.SOON, "namemc", confidence=Confidence.ESTIMATED, available_at=utcnow() + timedelta(days=2))

    db, job_id = staged(tmp_path, ["alpha", "bravo"])
    asyncio.run(make_scanner(db, job_id, FakeProvider(), enrichers=[Enricher()]).run())
    result = db.get_result("alpha")
    assert result.status is Status.SOON
    assert result.provider == "minecraft+namemc"
    assert db.get_result("bravo").status is Status.TAKEN
    db.close()


def test_scan_against_mocked_mojang_with_429(tmp_path, clock):
    """End to end: real MinecraftProvider + scanner, mocked HTTP that rate-limits once."""
    taken = {"bravo", "charlie", "delta"}
    calls = []

    def handler(request):
        calls.append(clock.now)
        if len(calls) == 2:
            return httpx.Response(429, headers={"Retry-After": "4"})
        names = json.loads(request.content)
        return httpx.Response(200, json=[{"id": "f" * 32, "name": n.upper()} for n in names if n in taken])

    config = AppConfig()
    provider = MinecraftProvider(
        config.providers.minecraft, config.scanner, client=mock_client(handler), sleep=clock.sleep, clock=clock, rng=random.Random(0)
    )
    db, job_id = staged(tmp_path)

    async def go():
        async with provider:
            return await make_scanner(db, job_id, provider).run()

    outcome = asyncio.run(go())
    assert outcome.completed
    assert provider.request_stats.rate_limited == 1
    assert provider.limiter.penalties == 1
    assert len(calls) == 3  # 2 bulk batches of 10 names + 1 retry
    assert db.get_result("bravo").display_name == "BRAVO"
    assert db.get_result("alpha").status is Status.AVAILABLE
    db.close()
