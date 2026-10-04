from datetime import timedelta

import pytest

from minecraft_finder.database import Database
from minecraft_finder.models import CheckResult, Confidence, Status
from minecraft_finder.timeutil import utcnow
from minecraft_finder.wordlist import Candidate


def make(name, status, provider="minecraft", **kw):
    return CheckResult(username=name, status=status, provider=provider, **kw)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "data" / "results.db")
    yield database
    database.close()


def test_roundtrip_preserves_fields(db):
    at = utcnow() + timedelta(days=3)
    original = make(
        "Daylight", Status.SOON, confidence=Confidence.ESTIMATED, available_at=at,
        detail="held", quality_score=71.5, source_word="daylight",
    )
    db.upsert_result(original)
    db.commit()
    loaded = db.get_result("DAYLIGHT")
    assert loaded is not None
    assert (loaded.username, loaded.display_name, loaded.status, loaded.confidence) == (
        "daylight", "Daylight", Status.SOON, Confidence.ESTIMATED,
    )
    assert abs((loaded.available_at - at).total_seconds()) < 0.001
    assert loaded.quality_score == 71.5


def test_cache_ttl(db):
    db.upsert_result(make("glacier", Status.TAKEN, checked_at=utcnow() - timedelta(hours=2)))
    assert db.get_fresh(["glacier"], max_age=timedelta(hours=1)) == {}
    assert "glacier" in db.get_fresh(["Glacier"], max_age=timedelta(hours=3))


def test_zero_ttl_disables_cache(db):
    db.upsert_result(make("glacier", Status.TAKEN))
    assert db.get_fresh(["glacier"], max_age=timedelta(0)) == {}


def test_errors_and_unknown_are_never_served_from_cache(db):
    db.upsert_results([make("broken", Status.ERROR, last_error="timeout"), make("unsure", Status.UNKNOWN)])
    assert db.get_fresh(["broken", "unsure"], max_age=timedelta(days=1)) == {}


def test_cache_is_per_primary_provider(db):
    db.upsert_results([make("demoname", Status.TAKEN, provider="demo"), make("enriched", Status.SOON, provider="minecraft+namemc")])
    fresh = db.get_fresh(["demoname", "enriched"], max_age=timedelta(days=1), provider="minecraft")
    assert set(fresh) == {"enriched"}


def test_soon_with_passed_estimate_is_stale(db):
    db.upsert_result(make("dusk", Status.SOON, confidence=Confidence.ESTIMATED, available_at=utcnow() - timedelta(minutes=1)))
    assert db.get_fresh(["dusk"], max_age=timedelta(days=1)) == {}


def test_error_does_not_overwrite_a_real_answer(db):
    first = make("notch", Status.TAKEN, display_name="Notch", uuid="069a79f444e94726a5befca90e38aaf5")
    db.upsert_result(first)
    db.upsert_result(make("notch", Status.ERROR, last_error="HTTP 503"))
    loaded = db.get_result("notch")
    assert loaded.status is Status.TAKEN
    assert loaded.last_error == "HTTP 503"
    assert loaded.checked_at == pytest.approx(first.checked_at, abs=timedelta(milliseconds=1))


def test_job_staging_dedupes_on_disk_and_tracks_progress(db):
    job = db.create_job("job1", "words.txt", {"transform": "lowercase"})
    assert not job.staged
    candidates = [Candidate(name, name, i) for i, name in enumerate(["alpha", "Alpha", "bravo", "charlie", "ALPHA", "delta"], 1)]
    total = db.stage_items("job1", candidates)
    db.mark_staged("job1", total, {"lines": 6})
    assert total == 4
    assert db.get_job("job1").staged

    first_page = db.pending_items("job1", after_seq=-1, limit=2)
    assert [item.username for item in first_page] == ["alpha", "bravo"]
    second_page = db.pending_items("job1", after_seq=first_page[-1].seq, limit=10)
    assert [item.username for item in second_page] == ["charlie", "delta"]

    db.upsert_results([make("alpha", Status.AVAILABLE), make("bravo", Status.TAKEN)])
    db.mark_done("job1", ["alpha", "bravo"])
    db.commit()
    assert db.job_progress("job1") == (4, 2)
    counts, confirmed = db.job_status_counts("job1")
    assert counts == {Status.AVAILABLE: 1, Status.TAKEN: 1}
    assert confirmed == 0
    assert [item.username for item in db.pending_items("job1", after_seq=-1, limit=10)] == ["charlie", "delta"]

    db.reset_job("job1")
    assert db.job_progress("job1") == (4, 0)


def test_iter_results_sorting_and_filters(db):
    db.upsert_results(
        [
            make("zebra", Status.AVAILABLE, quality_score=50.0),
            make("fox", Status.AVAILABLE, quality_score=90.0, confidence=Confidence.CONFIRMED),
            make("lantern", Status.SOON, quality_score=70.0),
            make("notch", Status.TAKEN, quality_score=95.0),
        ]
    )
    names = [r.username for r in db.iter_results(statuses={Status.AVAILABLE, Status.SOON}, sort="quality")]
    assert names == ["fox", "lantern", "zebra"]
    assert [r.username for r in db.iter_results(statuses={Status.AVAILABLE}, sort="name")] == ["fox", "zebra"]
    assert [r.username for r in db.iter_results(min_score=80)] == ["notch", "fox"]
    assert [r.username for r in db.iter_results(statuses={Status.AVAILABLE}, confirmed_only=True)] == ["fox"]
    assert [r.username for r in db.iter_results(max_length=5, sort="name")] == ["fox", "notch", "zebra"]
    assert len(list(db.iter_results(limit=2))) == 2


def test_delete_and_jobs_cleanup(db):
    db.upsert_results([make("broken", Status.ERROR), make("fine", Status.TAKEN)])
    db.commit()
    with pytest.raises(ValueError):
        db.delete_results()
    assert db.delete_results(statuses={Status.ERROR}) == 1
    db.create_job("done", "a.txt", {})
    db.create_job("open", "b.txt", {})
    db.finish_job("done")
    assert db.count_jobs(finished_only=True) == 1
    assert db.delete_jobs(finished_only=True) == 1
    assert db.get_job("done") is None
    assert db.get_job("open") is not None


def test_data_persists_across_connections(tmp_path):
    path = tmp_path / "r.db"
    with Database(path) as first:
        first.upsert_result(make("glacier", Status.AVAILABLE))
        first.commit()
    with Database(path) as second:
        assert second.get_result("glacier").status is Status.AVAILABLE
        assert second.schema_version() == 1


def test_count_uncached_pending_matches_cache_rules(db):
    db.create_job("job", "w.txt", {})
    names = ["fresh", "stale", "error", "other", "passed", "missing"]
    db.stage_items("job", [Candidate(n, n, i) for i, n in enumerate(names, 1)])
    old = utcnow() - timedelta(days=3)
    db.upsert_results(
        [
            make("fresh", Status.TAKEN),
            make("stale", Status.TAKEN, checked_at=old),
            make("error", Status.ERROR),
            make("other", Status.TAKEN, provider="demo"),
            make("passed", Status.SOON, available_at=utcnow() - timedelta(minutes=5)),
        ]
    )
    db.commit()
    assert db.count_uncached_pending("job", max_age=timedelta(days=1), provider="minecraft") == 5
    assert db.count_uncached_pending("job", max_age=timedelta(0), provider="minecraft") == 6
    fresh = db.get_fresh(names, max_age=timedelta(days=1), provider="minecraft")
    assert set(fresh) == {"fresh"}  # same rules as the SQL count
