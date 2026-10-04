"""Local, offline "attractiveness" score for usernames (0-100).

The score is only used for sorting; nothing is ever hidden because of a low score.

Components:

* length (max 40)           — shorter names are rarer and easier to remember
* characters (max 20)       — letters only beats digits and underscores
* dictionary word (max 15)  — the name is exactly a (letters-only) word from the scanned word list
* pronounceability (max 25) — vowel balance, consonant clusters, repeated letters
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

LENGTH_POINTS = {
    3: 40.0, 4: 38.0, 5: 35.0, 6: 31.0, 7: 27.0, 8: 23.0, 9: 19.0, 10: 15.0,
    11: 12.0, 12: 9.0, 13: 7.0, 14: 5.0, 15: 3.0, 16: 2.0,
}  # fmt: skip
MAX_CHARACTER_POINTS = 20.0
PENALTY_PER_DIGIT = 6.0
PENALTY_PER_UNDERSCORE = 6.0
DICTIONARY_POINTS = 15.0
MAX_PRONOUNCEABILITY_POINTS = 25.0
VOWELS = frozenset("aeiouy")


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    length: float
    characters: float
    dictionary: float
    pronounceability: float

    @property
    def total(self) -> float:
        raw = self.length + self.characters + self.dictionary + self.pronounceability
        return round(min(100.0, raw), 1)


def _longest_run(text: str, predicate: Callable[[str], bool]) -> int:
    best = run = 0
    for ch in text:
        run = run + 1 if predicate(ch) else 0
        best = max(best, run)
    return best


def pronounceability(name: str) -> float:
    """Heuristic in ``[0, 1]``; 1 means "reads like a word"."""
    letters = "".join(ch for ch in name.lower() if ch.isalpha())
    if not letters:
        return 0.0
    vowel_ratio = sum(ch in VOWELS for ch in letters) / len(letters)
    if 0.3 <= vowel_ratio <= 0.6:
        ratio_score = 1.0
    elif vowel_ratio < 0.3:
        ratio_score = vowel_ratio / 0.3
    else:
        ratio_score = max(0.0, 1.0 - (vowel_ratio - 0.6) / 0.4)

    consonant_run = _longest_run(letters, lambda c: c not in VOWELS)
    cluster_score = {0: 1.0, 1: 1.0, 2: 1.0, 3: 0.7, 4: 0.35}.get(consonant_run, 0.0)
    vowel_run = _longest_run(letters, lambda c: c in VOWELS)
    vowel_score = 1.0 if vowel_run <= 2 else 0.6 if vowel_run == 3 else 0.2

    score = 0.5 * ratio_score + 0.35 * cluster_score + 0.15 * vowel_score
    if any(a == b == c for a, b, c in zip(letters, letters[1:], letters[2:])):
        score *= 0.5  # "aaa", "zzz", ...
    return round(score, 4)


def score_breakdown(username: str, *, dictionary_word: bool = False) -> ScoreBreakdown:
    name = username.lower()
    digits = sum(ch.isdigit() for ch in name)
    underscores = name.count("_")
    characters = max(
        0.0,
        MAX_CHARACTER_POINTS - PENALTY_PER_DIGIT * digits - PENALTY_PER_UNDERSCORE * underscores,
    )
    return ScoreBreakdown(
        length=LENGTH_POINTS.get(len(name), 0.0),
        characters=characters,
        # A word-list entry containing digits or underscores is not a real dictionary word.
        dictionary=DICTIONARY_POINTS if dictionary_word and name.isalpha() else 0.0,
        pronounceability=round(pronounceability(name) * MAX_PRONOUNCEABILITY_POINTS, 2),
    )


def quality_score(username: str, *, dictionary_word: bool = False) -> float:
    return score_breakdown(username, dictionary_word=dictionary_word).total
