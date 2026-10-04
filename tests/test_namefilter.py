"""Offensive-name filter: what it catches, what it must leave alone, and how it is applied."""

import asyncio

import pytest
from typer.testing import CliRunner

from minecraft_finder.availability import headline, short_label
from minecraft_finder.cli import app
from minecraft_finder.database import Database
from minecraft_finder.models import CheckResult, Confidence, Status
from minecraft_finder.namefilter import flag_if_offensive, is_offensive, reclassify_existing

from .test_scanner import FakeProvider, make_scanner, staged


@pytest.mark.parametrize(
    "name",
    [
        # from a real export: short, real words that Mojang refuses
        "anus", "arses", "boner", "boners", "boobs", "booby", "boobies", "bitchy", "bitching", "bastardly",
        "asshole", "clitoris", "bullshits", "nigger", "chinkiest",
        # endings and fragments
        "raping", "raped", "titties", "anality", "coitally", "cocksucker", "xxbitchxx", "fuckboy", "shithead",
        "Boobs",
    ],
)  # fmt: skip
def test_offensive_names_are_caught(name):
    assert is_offensive(name)


@pytest.mark.parametrize(
    "name",
    [
        "bass", "bassi", "class", "assess", "assassin", "passion", "therapist", "therapists", "scrape", "grape",
        "drape", "rapping", "spicy", "cockatoo", "peacock", "cocktail", "cockerels", "canal", "analysis", "title",
        "dickens", "cumin", "accumulate", "circumflex", "japan", "arsenal", "arsenic", "swanky", "spoon",
        "raccoon", "cocoon", "homes", "buttress", "ambassador", "glacier", "lantern",
    ],
)  # fmt: skip
def test_innocent_lookalikes_are_allowed(name):
    assert not is_offensive(name)


def test_source_word_is_checked_for_affixed_names():
    assert is_offensive("boobsmc", source_word="boobs")
    assert not is_offensive("glaciermc", source_word="glacier")


def test_flagged_results_are_probably_blocked():
    flagged = flag_if_offensive(CheckResult("boner", Status.AVAILABLE, "minecraft", confidence=Confidence.UNVERIFIED))
    assert (flagged.status, flagged.confidence) == (Status.BLOCKED, Confidence.UNVERIFIED)
    assert headline(flagged) == "PROBABLY NOT ALLOWED"
    assert short_label(flagged) == "probably not allowed"
    taken = CheckResult("boner", Status.TAKEN, "minecraft")
    assert flag_if_offensive(taken) is taken
    confirmed = CheckResult("x", Status.BLOCKED, "minecraft", confidence=Confidence.CONFIRMED)
    assert headline(confirmed) == "NOT ALLOWED"


def test_scan_skips_offensive_names_without_requests(tmp_path):
    db, job_id = staged(tmp_path, ["boner", "glacier", "bitchy", "echo"])
    provider = FakeProvider()
    outcome = asyncio.run(make_scanner(db, job_id, provider).run())
    assert outcome.completed
    assert sorted(provider.calls) == ["echo", "glacier"]
    boner = db.get_result("boner")
    assert (boner.status, boner.confidence) == (Status.BLOCKED, Confidence.UNVERIFIED)
    db.close()


def test_filter_can_be_disabled(tmp_path):
    db, job_id = staged(tmp_path, ["boner", "glacier"])
    provider = FakeProvider()
    scanner = make_scanner(db, job_id, provider)
    scanner.options = type(scanner.options)(workers=2, flush_interval=0.05, filter_offensive=False)
    asyncio.run(scanner.run())
    assert sorted(provider.calls) == ["boner", "glacier"]
    db.close()


def test_existing_results_are_reclassified(tmp_path):
    with Database(tmp_path / "r.db") as db:
        db.upsert_results(
            [
                CheckResult("boobs", Status.AVAILABLE, "minecraft", confidence=Confidence.UNVERIFIED),
                CheckResult("glacier", Status.AVAILABLE, "minecraft", confidence=Confidence.UNVERIFIED),
                CheckResult("boner", Status.TAKEN, "minecraft"),
            ]
        )
        db.commit()
        assert reclassify_existing(db) == 1
        assert db.get_result("boobs").status is Status.BLOCKED
        assert db.get_result("glacier").status is Status.AVAILABLE
        assert db.get_result("boner").status is Status.TAKEN
        assert reclassify_existing(db) == 0  # idempotent


def test_export_leaves_out_offensive_names(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with Database(tmp_path / "data" / "results.db") as db:
        db.upsert_results(
            [
                CheckResult("anus", Status.AVAILABLE, "minecraft", confidence=Confidence.UNVERIFIED, quality_score=99),
                CheckResult("glacier", Status.AVAILABLE, "minecraft", confidence=Confidence.UNVERIFIED, quality_score=80),
            ]
        )
        db.commit()
    result = CliRunner().invoke(app, ["export", "--format", "txt", "-o", "-"])
    assert result.output.split() == ["glacier"]
