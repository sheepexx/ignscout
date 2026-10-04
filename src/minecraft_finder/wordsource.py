"""Ready-made word lists, so nobody has to go looking for one."""

from __future__ import annotations

import enum
import itertools
import os
import re
import string
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx

from . import DEFAULT_USER_AGENT

WORDLISTS_DIR = Path("wordlists")
#: ~370,000 English words, one per line. Public domain (The Unlicense), github.com/dwyl/english-words.
ENGLISH_WORDS_URL = "https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt"
ENGLISH_WORDS_PATH = WORDLISTS_DIR / "english.txt"
ENGLISH_WORDS_APPROX = 370_000

#: Lengths for which every possible name can be listed (5 letters alone would be 11.9 million).
SHORT_NAME_LENGTHS = (3, 4)
LETTERS = string.ascii_lowercase
DIGITS = string.digits
_SHORT_LIST_RE = re.compile(r"all-(\d)-(?:letters|with-numbers|with-underscores)\.txt")


class ShortNameKind(enum.StrEnum):
    """Three groups of short names that never overlap; together they are every possible name."""

    LETTERS = "letters"  # a-z only
    NUMBERS = "numbers"  # a-z and 0-9, at least one digit
    UNDERSCORES = "underscores"  # a-z, 0-9 and _, at least one underscore


def short_name_count(length: int, kind: ShortNameKind) -> int:
    letters = len(LETTERS) ** length
    with_digits = (len(LETTERS) + len(DIGITS)) ** length
    if kind is ShortNameKind.LETTERS:
        return letters
    if kind is ShortNameKind.NUMBERS:
        return with_digits - letters
    return (len(LETTERS) + len(DIGITS) + 1) ** length - with_digits


def iter_short_names(length: int, kind: ShortNameKind) -> Iterator[str]:
    """Every name of ``length`` characters in ``kind``, lower-case, in a fixed order."""
    alphabet = LETTERS if kind is ShortNameKind.LETTERS else LETTERS + DIGITS
    if kind is ShortNameKind.UNDERSCORES:
        alphabet += "_"
    for chars in itertools.product(alphabet, repeat=length):
        name = "".join(chars)
        if kind is ShortNameKind.NUMBERS and not any(ch in DIGITS for ch in name):
            continue
        if kind is ShortNameKind.UNDERSCORES and "_" not in name:
            continue
        yield name


def short_name_length(path: Path) -> int | None:
    """The name length if ``path`` is a list written by :func:`short_name_wordlist`, else None."""
    match = _SHORT_LIST_RE.fullmatch(path.name)
    return int(match[1]) if match else None


def short_name_wordlist(length: int, kind: ShortNameKind, directory: Path = WORDLISTS_DIR) -> Path:
    """A word list of every ``length``-character name in ``kind``. Written once, then reused."""
    if length not in SHORT_NAME_LENGTHS:
        raise ValueError(f"only names of {' or '.join(map(str, SHORT_NAME_LENGTHS))} characters can be listed")
    label = "letters" if kind is ShortNameKind.LETTERS else f"with-{kind.value}"
    path = directory / f"all-{length}-{label}.txt"
    # Every line is the name plus "\n", so the size tells whether the file is complete and current.
    if path.is_file() and path.stat().st_size == short_name_count(length, kind) * (length + 1):
        return path
    directory.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    try:
        with partial.open("w", encoding="utf-8", newline="\n") as handle:
            handle.writelines(f"{name}\n" for name in iter_short_names(length, kind))
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)
    return path


class DownloadError(Exception):
    pass


def sample_wordlist() -> Path | None:
    """The small sample list shipped in the repository (absent in non-editable installs)."""
    candidate = Path(__file__).resolve().parents[2] / "examples" / "words-sample.txt"
    return candidate if candidate.is_file() else None


def download_wordlist(
    dest: Path = ENGLISH_WORDS_PATH,
    *,
    url: str = ENGLISH_WORDS_URL,
    client: httpx.Client | None = None,
    on_progress: Callable[[int, int | None], None] | None = None,
    min_lines: int = 100_000,
) -> int:
    """Download a word list to ``dest`` (atomically) and return its number of lines."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=30.0, follow_redirects=True, headers={"User-Agent": DEFAULT_USER_AGENT})
    try:
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise DownloadError(f"the download failed (HTTP {response.status_code})")
            if "html" in response.headers.get("content-type", "").lower():
                raise DownloadError("the server sent a web page instead of a word list")
            total = int(response.headers.get("content-length") or 0) or None
            received = 0
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
                    received += len(chunk)
                    if on_progress is not None:
                        on_progress(received, total)
        with partial.open("rb") as handle:
            lines = sum(1 for line in handle if line.strip())
        if lines < min_lines:
            raise DownloadError(f"the downloaded file has only {lines:,} lines; it does not look like the word list")
        os.replace(partial, dest)
        return lines
    except httpx.HTTPError as exc:
        raise DownloadError(f"the download failed ({type(exc).__name__}); check your internet connection") from exc
    finally:
        partial.unlink(missing_ok=True)
        if owns_client:
            client.close()
