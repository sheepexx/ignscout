"""Word-list download, keep-awake and console-symbol selection."""

import sys

import httpx
import pytest

from minecraft_finder import keepawake
from minecraft_finder.models import Status
from minecraft_finder.ui import simple_glyphs, spinner_name, symbol
from minecraft_finder.validator import is_valid_username
from minecraft_finder.wordsource import (
    DownloadError,
    ShortNameKind,
    download_wordlist,
    iter_short_names,
    sample_wordlist,
    short_name_count,
    short_name_length,
    short_name_wordlist,
)


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_download_writes_file_atomically(tmp_path):
    words = "\n".join(f"word{i}" for i in range(1000)) + "\n"
    progress = []
    dest = tmp_path / "wordlists" / "english.txt"
    lines = download_wordlist(
        dest,
        client=client(lambda r: httpx.Response(200, text=words, headers={"content-type": "text/plain"})),
        on_progress=lambda done, total: progress.append(done),
        min_lines=500,
    )
    assert lines == 1000
    assert dest.read_text().splitlines()[0] == "word0"
    assert progress and progress[-1] == len(words.encode())
    assert not list(dest.parent.glob("*.part"))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, text="nope"),
        httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"}),
        httpx.Response(200, text="just\nthree\nlines\n", headers={"content-type": "text/plain"}),
    ],
)
def test_bad_downloads_leave_nothing_behind(tmp_path, response):
    dest = tmp_path / "english.txt"
    with pytest.raises(DownloadError):
        download_wordlist(dest, client=client(lambda r: response), min_lines=500)
    assert not dest.exists()
    assert not list(tmp_path.glob("*.part"))


def test_network_errors_are_friendly(tmp_path):
    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(DownloadError, match="internet connection"):
        download_wordlist(tmp_path / "english.txt", client=client(handler))


def test_sample_wordlist_ships_with_the_repo():
    sample = sample_wordlist()
    assert sample is not None and sample.read_text(encoding="utf-8").strip()


def test_keep_awake():
    with keepawake.keep_awake(enabled=False) as active:
        assert active is False
    if sys.platform == "win32":
        assert keepawake.supported()
        with keepawake.keep_awake() as active:
            assert active is True


def test_symbol_modes(monkeypatch):
    monkeypatch.setenv("MCF_SYMBOLS", "simple")
    assert symbol(Status.AVAILABLE) == "√" and spinner_name() == "line"
    monkeypatch.setenv("MCF_SYMBOLS", "fancy")
    assert symbol(Status.AVAILABLE) == "✓" and spinner_name() == "dots"


def test_classic_windows_console_gets_simple_symbols(monkeypatch):
    monkeypatch.delenv("MCF_SYMBOLS", raising=False)
    for name in ("WT_SESSION", "TERM_PROGRAM", "TERM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert simple_glyphs()
    monkeypatch.setenv("WT_SESSION", "1")  # Windows Terminal
    assert not simple_glyphs()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WT_SESSION")
    assert not simple_glyphs()


def test_no_quick_edit_is_harmless_without_a_console():
    # Under pytest stdin is not a console window, so nothing may be changed (or crash).
    with keepawake.no_quick_edit() as changed:
        assert changed is False


@pytest.mark.parametrize("length", [3, 4])
def test_short_name_kinds_cover_every_name_exactly_once(length):
    groups = {kind: list(iter_short_names(length, kind)) for kind in ShortNameKind}
    for kind, names in groups.items():
        assert len(names) == len(set(names)) == short_name_count(length, kind)
    letters, numbers, underscores = (set(groups[kind]) for kind in ShortNameKind)
    assert not (letters & numbers) and not (letters & underscores) and not (numbers & underscores)
    assert len(letters | numbers | underscores) == 37**length  # a-z, 0-9 and _
    assert all(name.isalpha() for name in letters)
    assert all(any(ch.isdigit() for ch in name) and "_" not in name for name in numbers)
    assert all("_" in name and is_valid_username(name) for name in underscores)


def test_short_name_wordlist_is_written_once_and_repaired(tmp_path):
    path = short_name_wordlist(3, ShortNameKind.UNDERSCORES, tmp_path)
    assert path.name == "all-3-with-underscores.txt"
    names = path.read_text(encoding="utf-8").split()
    assert len(names) == 3997 and "a_b" in names
    stamp = path.stat().st_mtime_ns
    assert short_name_wordlist(3, ShortNameKind.UNDERSCORES, tmp_path) == path
    assert path.stat().st_mtime_ns == stamp  # reused, not rewritten
    path.write_text("a_b\n", encoding="utf-8")  # damaged or edited: written again
    assert len(short_name_wordlist(3, ShortNameKind.UNDERSCORES, tmp_path).read_text(encoding="utf-8").split()) == 3997
    assert not list(tmp_path.glob("*.part"))
    assert short_name_length(path) == 3
    assert short_name_length(tmp_path / "english.txt") is None
    with pytest.raises(ValueError):
        short_name_wordlist(5, ShortNameKind.LETTERS, tmp_path)
