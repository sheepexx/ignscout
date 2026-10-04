"""Only one scan per database; viewing results while a scan runs stays possible."""

import subprocess
import sys
import textwrap

from typer.testing import CliRunner

from minecraft_finder.cli import app
from minecraft_finder.scanlock import ScanLock, lock_path_for, scan_running

runner = CliRunner()

# Holds the lock from a *different process*, like a scan running in another window.
HOLDER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from minecraft_finder.scanlock import ScanLock
    lock = ScanLock(Path(sys.argv[1]))
    assert lock.acquire()
    print("locked", flush=True)
    sys.stdin.read()
    """
)


def hold_lock(db_path):
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_path_for(db_path))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    assert process.stdout.readline().strip() == "locked"
    return process


def test_lock_is_exclusive_and_released(tmp_path):
    db_path = tmp_path / "results.db"
    assert not scan_running(db_path)
    holder = hold_lock(db_path)
    try:
        assert scan_running(db_path)
        assert not ScanLock(lock_path_for(db_path)).acquire()
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)
    assert not scan_running(db_path)  # released when the other process exits
    with ScanLock(lock_path_for(db_path)) as lock:
        assert lock.acquire()


def test_second_scan_is_refused_but_viewing_works(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        "[scanner]\nrequests_per_second = 50\n\n[providers]\ndefault = 'demo'\n\n[providers.demo]\nlatency = 0.0\nerror_rate = 0.0\n",
        encoding="utf-8",
    )
    (tmp_path / "words.txt").write_text("alpha\nbravo\ncharlie\n", encoding="utf-8")
    assert runner.invoke(app, ["scan", "words.txt"]).exit_code == 0

    holder = hold_lock(tmp_path / "data" / "demo-results.db")
    try:
        refused = runner.invoke(app, ["scan", "words.txt", "--restart"])
        assert refused.exit_code == 1
        assert "already running" in refused.output

        menu = runner.invoke(app, [], input="2\n\n4\n\n0\n")  # try to search, then show results
        assert "A search is running in another window" in menu.output
        assert "Only one can run at a time" in menu.output
        assert "Continue my unfinished search" not in menu.output

        stats = runner.invoke(app, ["stats", "--demo"])
        assert stats.exit_code == 0 and "Results database" in stats.output
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)
