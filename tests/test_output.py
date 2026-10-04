import json
import os
from datetime import timedelta

from minecraft_finder.models import CheckResult, Confidence, Status
from minecraft_finder.output import AppendOnlyFile, ResultWriter, read_names
from minecraft_finder.timeutil import utcnow


def make(name, status, **kw):
    return CheckResult(username=name, status=status, provider="minecraft", **kw)


def test_writer_appends_discoveries_and_deduplicates(tmp_path):
    soon_at = utcnow() + timedelta(days=3)
    with ResultWriter(tmp_path) as writer:
        writer.record(make("glacier", Status.AVAILABLE, confidence=Confidence.UNVERIFIED))
        writer.record(make("glacier", Status.AVAILABLE), fresh=False)
        writer.record(make("daylight", Status.SOON, confidence=Confidence.ESTIMATED, available_at=soon_at))
        writer.record(make("dusk", Status.SOON))
        writer.record(make("notch", Status.TAKEN, display_name="Notch"))

    assert (tmp_path / "available.txt").read_text(encoding="utf-8") == "glacier\n"
    soon_lines = (tmp_path / "soon.txt").read_text(encoding="utf-8").splitlines()
    assert soon_lines[0].startswith("daylight\testimated 20")
    assert soon_lines[1] == "dusk\trelease unknown"
    assert len((tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()) == 4  # cached record skipped

    with ResultWriter(tmp_path) as again:  # a later run must not duplicate names
        again.record(make("Glacier", Status.AVAILABLE))
        again.record(make("orchard", Status.AVAILABLE))
    assert (tmp_path / "available.txt").read_text(encoding="utf-8").splitlines() == ["glacier", "orchard"]


def test_jsonl_records(tmp_path):
    with ResultWriter(tmp_path) as writer:
        writer.record(make("lantern", Status.AVAILABLE, confidence=Confidence.UNVERIFIED, quality_score=80.0))
    record = json.loads((tmp_path / "results.jsonl").read_text(encoding="utf-8"))
    assert record["username"] == "lantern"
    assert record["status"] == "available"
    assert record["confidence"] == "unverified"
    assert record["provider"] == "minecraft"
    assert record["checked_at"].endswith("Z")


def test_torn_last_line_is_terminated_before_appending(tmp_path):
    path = tmp_path / "available.txt"
    path.write_bytes(b"glacier\nlant")  # simulated crash mid-write
    handle = AppendOnlyFile(path)
    handle.write_line("orchard")
    handle.close()
    assert path.read_text(encoding="utf-8").splitlines() == ["glacier", "lant", "orchard"]
    assert "orchard" in read_names(path)


def test_discoveries_are_fsynced_immediately(tmp_path, monkeypatch):
    calls = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd)))
    handle = AppendOnlyFile(tmp_path / "x.txt", fsync_every=1)
    handle.write_line("a")
    handle.write_line("b")
    assert len(calls) == 2
    handle.close()


def test_newlines_in_records_cannot_split_lines(tmp_path):
    handle = AppendOnlyFile(tmp_path / "x.txt")
    handle.write_line("evil\nname")
    handle.close()
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "evil name\n"
