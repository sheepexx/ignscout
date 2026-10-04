"""Streaming word-list processing: transform -> prefix/suffix -> validate -> filter.

Files are read line by line, so memory use does not depend on the size of the
word list. De-duplication of very large lists happens on disk, in SQLite (see
``Database.stage_items``); :func:`dedupe` is the in-memory variant for small inputs.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .validator import is_valid_username


class Transform(enum.StrEnum):
    NONE = "none"  # strip surrounding whitespace only
    LOWERCASE = "lowercase"  # strip + lowercase
    COMPACT = "compact"  # lowercase, fold accents, drop spaces/apostrophes/hyphens/dots


# Characters "compact" removes: whitespace, apostrophes and quotes, hyphens and dashes, dots.
_COMPACT_REMOVE = re.compile(r"[\s'\"`´‘’ʼ\-‐-―.]+")


def fold_accents(text: str) -> str:
    """``café`` -> ``cafe``. Characters without an ASCII base are kept (and later rejected)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def transform_word(word: str, mode: Transform) -> str:
    word = word.strip()
    if mode is Transform.NONE:
        return word
    if mode is Transform.LOWERCASE:
        return word.lower()
    return _COMPACT_REMOVE.sub("", fold_accents(word)).lower()


@dataclass(frozen=True)
class CandidateFilter:
    """Filters applied to the *final* candidate username (after transform, prefix and suffix)."""

    min_length: int | None = None
    max_length: int | None = None
    starts_with: str | None = None
    ends_with: str | None = None
    contains: str | None = None
    regex: str | None = None
    exclude_regex: str | None = None
    _regex: re.Pattern[str] | None = field(init=False, repr=False, compare=False, default=None)
    _exclude: re.Pattern[str] | None = field(init=False, repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("--min-length cannot be greater than --max-length")
        # re.error propagates so the CLI can report the bad pattern.
        object.__setattr__(self, "_regex", re.compile(self.regex) if self.regex else None)
        object.__setattr__(
            self, "_exclude", re.compile(self.exclude_regex) if self.exclude_regex else None
        )

    def matches(self, username: str) -> bool:
        length = len(username)
        if self.min_length is not None and length < self.min_length:
            return False
        if self.max_length is not None and length > self.max_length:
            return False
        lowered = username.lower()
        if self.starts_with and not lowered.startswith(self.starts_with.lower()):
            return False
        if self.ends_with and not lowered.endswith(self.ends_with.lower()):
            return False
        if self.contains and self.contains.lower() not in lowered:
            return False
        if self._regex is not None and not self._regex.search(username):
            return False
        if self._exclude is not None and self._exclude.search(username):
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if not key.startswith("_") and value is not None
        }


@dataclass(frozen=True)
class CandidateOptions:
    transform: Transform = Transform.LOWERCASE
    prefix: str = ""
    suffix: str = ""
    filter: CandidateFilter = field(default_factory=CandidateFilter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "transform": self.transform.value,
            "prefix": self.prefix,
            "suffix": self.suffix,
            "filter": self.filter.as_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateOptions:
        """Inverse of :meth:`as_dict` (used to resume a saved scan job)."""
        return cls(
            transform=Transform(data.get("transform", Transform.LOWERCASE.value)),
            prefix=data.get("prefix", ""),
            suffix=data.get("suffix", ""),
            filter=CandidateFilter(**data.get("filter", {})),
        )

    def scan_arguments(self) -> dict[str, Any]:
        """Keyword arguments for the ``scan`` command that reproduce these options."""
        return {
            "transform": self.transform,
            "prefix": self.prefix,
            "suffix": self.suffix,
            "min_length": self.filter.min_length,
            "max_length": self.filter.max_length,
            "starts_with": self.filter.starts_with,
            "ends_with": self.filter.ends_with,
            "contains": self.filter.contains,
            "regex": self.filter.regex,
            "exclude_regex": self.filter.exclude_regex,
        }


@dataclass(slots=True)
class Candidate:
    username: str
    source_word: str
    line_no: int


@dataclass(slots=True)
class ReadStats:
    lines: int = 0
    chars: int = 0  # approximate bytes read, for progress bars
    blank: int = 0
    invalid: int = 0
    filtered: int = 0
    accepted: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "lines": self.lines,
            "blank": self.blank,
            "invalid": self.invalid,
            "filtered": self.filtered,
            "accepted": self.accepted,
        }


def iter_lines(path: Path) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, raw_line)``. Handles a UTF-8 BOM and replaces undecodable bytes."""
    with path.open("r", encoding="utf-8-sig", errors="replace", newline=None) as handle:
        yield from enumerate(handle, start=1)


def iter_candidates(
    path: Path,
    options: CandidateOptions,
    *,
    stats: ReadStats | None = None,
    on_progress: Callable[[ReadStats], None] | None = None,
    progress_every: int = 20_000,
) -> Iterator[Candidate]:
    """Stream valid, filtered candidates from a word list (duplicates included)."""
    stats = stats if stats is not None else ReadStats()
    for line_no, raw in iter_lines(path):
        stats.lines += 1
        stats.chars += len(raw)
        if on_progress is not None and line_no % progress_every == 0:
            on_progress(stats)
        word = raw.strip()
        if not word or word.startswith("#"):
            stats.blank += 1
            continue
        username = f"{options.prefix}{transform_word(word, options.transform)}{options.suffix}"
        if not is_valid_username(username):
            stats.invalid += 1
            continue
        if not options.filter.matches(username):
            stats.filtered += 1
            continue
        stats.accepted += 1
        yield Candidate(username=username, source_word=word, line_no=line_no)
    if on_progress is not None:
        on_progress(stats)


def dedupe(candidates: Iterable[Candidate]) -> Iterator[Candidate]:
    """In-memory, case-insensitive de-duplication (keeps the first occurrence)."""
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.username.lower()
        if key not in seen:
            seen.add(key)
            yield candidate


def is_dictionary_word(username: str, source_word: str | None) -> bool:
    """True if the username is exactly the (compacted) source word: no prefix or suffix added."""
    if not source_word:
        return False
    return transform_word(source_word, Transform.COMPACT) == username.lower()


def wordlist_fingerprint(path: Path, options: CandidateOptions) -> str:
    """Stable identifier of a scan: same file + same options -> same job (enables resume)."""
    resolved = str(path.resolve())
    if os.name == "nt":
        resolved = resolved.lower()
    payload = {"path": resolved, "size": path.stat().st_size, "options": options.as_dict()}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:16]
