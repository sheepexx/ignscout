"""The interactive menu, driven with scripted keyboard input (demo provider, no network)."""

import pytest
from typer.testing import CliRunner

from minecraft_finder.cli import app
from minecraft_finder.database import Database
from minecraft_finder.models import Status
from minecraft_finder.wordlist import CandidateFilter, CandidateOptions, Transform

runner = CliRunner()


def write_config(path, error_rate=0.0):
    path.write_text(
        "[scanner]\nrequests_per_second = 50\n\n[providers]\ndefault = 'demo'\n\n"
        f"[providers.demo]\nlatency = 0.0\nerror_rate = {error_rate}\n",
        encoding="utf-8",
    )


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_config(tmp_path / "config.toml")
    (tmp_path / "my words.txt").write_text(
        "\n".join(f"{a}{b}{c}word" for a in "abcde" for b in "fghij" for c in "klmno") + "\n", encoding="utf-8"
    )
    # Makes the "open the folder?" question appear deterministically.
    (tmp_path / "output" / "demo").mkdir(parents=True)
    (tmp_path / "output" / "demo" / "available.txt").write_text("", encoding="utf-8")
    return tmp_path


def menu(*answers: str):
    return runner.invoke(app, [], input="\n".join(answers) + "\n", catch_exceptions=False)


def test_no_arguments_opens_the_menu(workdir):
    result = menu("7", "", "0")
    assert result.exit_code == 0
    assert "What do you want to do?" in result.output
    assert "How it works" in result.output
    assert "Bye!" in result.output


def test_check_from_the_menu(workdir):
    result = menu("1", "ab glacier", "", "0")
    assert "INVALID" in result.output
    assert "glacier" in result.output


def test_search_own_wordlist_then_show_and_save(workdir):
    path = workdir / "my words.txt"
    result = menu(
        "2",           # search
        "2",           # my own word list
        f'"{path}"',   # dragged-in path, with quotes
        "1",           # any length
        "n",           # no letters before/after
        "n",           # no numbers/underscores
        "y",           # start now
        "n",           # don't open the folder
        "",            # back to menu
        "0",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "Start now?" in result.output
    assert "Scan complete" in result.output
    with Database(workdir / "data" / "demo-results.db") as db:
        assert db.total_results() == 125

    shown = menu("4", "", "", "5", "1", "", "n", "", "0")  # Enter keeps the default sort (best first)
    assert "Your best finds" in shown.output
    assert (workdir / "output" / "demo" / "export.csv").exists()


def test_show_and_save_sorted_by_length(workdir):
    words = ["glacierfall", "sun", "meadowlark", "frost", "ab", "quartzite", "oak", "riverbend", "maple", "cobble"]
    (workdir / "words.txt").write_text("\n".join(words) + "\n", encoding="utf-8")
    runner.invoke(app, ["scan", "words.txt"], catch_exceptions=False)

    shown = menu("4", "2", "", "0")  # show, shortest first
    assert "Your finds (shortest first)" in shown.output

    menu("5", "2", "2", "n", "", "0")  # save as a simple list, shortest first
    names = (workdir / "output" / "demo" / "export.txt").read_text(encoding="utf-8").split()
    assert names
    assert [len(name) for name in names] == sorted(len(name) for name in names)


def test_search_with_suffix_and_custom_length(workdir):
    path = workdir / "my words.txt"
    result = menu("2", "2", str(path), "4", "3", "10", "y", "", "x!", "mc", "n", "y", "n", "", "0")
    assert "Only letters, numbers and _" in result.output  # "x!" was rejected and asked again
    assert "Scan complete" in result.output
    with Database(workdir / "data" / "demo-results.db") as db:
        names = [r.username for r in db.iter_results(sort="name")]
    assert names and all(name.endswith("mc") and len(name) <= 10 for name in names)


def test_search_every_short_name(workdir, monkeypatch):
    from minecraft_finder import wordsource

    monkeypatch.setattr(wordsource, "LETTERS", "abc")  # 27 names instead of 17,576
    result = menu(
        "2",  # search
        "3",  # every short name
        "1",  # 3 characters
        "1",  # only letters
        "y",  # start now
        "n",  # don't open the folder
        "",
        "0",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "The three never overlap" in result.output
    assert "17,576" not in result.output and "27 names" in result.output  # counts follow the alphabet
    assert "Scan complete" in result.output
    assert len((workdir / "wordlists" / "all-3-letters.txt").read_text(encoding="utf-8").split()) == 27
    with Database(workdir / "data" / "demo-results.db") as db:
        assert db.total_results() == 27


def test_paused_search_can_be_continued(workdir):
    write_config(workdir / "config.toml", error_rate=1.0)  # every check fails -> search stays unfinished
    runner.invoke(app, ["scan", "my words.txt"], catch_exceptions=False)
    write_config(workdir / "config.toml", error_rate=0.0)
    result = menu("3", "n", "", "0")
    assert "Continue my unfinished search" in result.output
    assert "Scan complete" in result.output


def test_missing_file_is_asked_again(workdir):
    result = menu("2", "2", "C:/does/not/exist.txt", "", "0")
    assert "There is no file at" in result.output


def test_candidate_options_roundtrip():
    options = CandidateOptions(
        transform=Transform.COMPACT, prefix="x", suffix="mc", filter=CandidateFilter(min_length=4, regex="^x[a-z]+mc$")
    )
    assert CandidateOptions.from_dict(options.as_dict()) == options
    assert options.scan_arguments()["min_length"] == 4


def test_status_enum_has_menu_symbols():
    from minecraft_finder.ui import symbol

    assert all(symbol(status) for status in Status)


def test_declining_does_not_leave_a_paused_search(workdir):
    path = workdir / "my words.txt"
    result = menu("2", "2", str(path), "1", "n", "n", "n", "", "0")
    assert "Not started" in result.output
    assert "Time needed" in result.output  # the plain-language panel
    assert "Continue my unfinished search" not in result.output
    with Database(workdir / "data" / "demo-results.db") as db:
        assert db.list_jobs() == []
