import itertools
import re

import pytest

from minecraft_finder.wordlist import (
    Candidate,
    CandidateFilter,
    CandidateOptions,
    ReadStats,
    Transform,
    dedupe,
    is_dictionary_word,
    iter_candidates,
    transform_word,
    wordlist_fingerprint,
)


@pytest.mark.parametrize(
    ("word", "mode", "expected"),
    [
        ("  Hello ", Transform.NONE, "Hello"),
        ("  Hello ", Transform.LOWERCASE, "hello"),
        ("O'Brien", Transform.COMPACT, "obrien"),
        ("ice-cream", Transform.COMPACT, "icecream"),
        ("New York", Transform.COMPACT, "newyork"),
        ("café", Transform.COMPACT, "cafe"),
        ("don’t", Transform.COMPACT, "dont"),
        ("e.g.", Transform.COMPACT, "eg"),
        ("Hello World", Transform.LOWERCASE, "hello world"),
    ],
)
def test_transform_word(word, mode, expected):
    assert transform_word(word, mode) == expected


def test_filter_length_and_text():
    f = CandidateFilter(min_length=4, max_length=7, starts_with="G", ends_with="r", contains="ac")
    assert f.matches("glacier")
    assert not f.matches("gar")  # too short
    assert not f.matches("glaciers")  # too long and wrong ending
    assert not f.matches("blacker")  # wrong start
    assert not f.matches("glimmer")  # no "ac"


def test_filter_regex_and_exclude():
    f = CandidateFilter(regex=r"^[a-z]+$", exclude_regex=r"[0-9_]")
    assert f.matches("velvet")
    assert not f.matches("velvet_")
    assert not f.matches("Velvet")


def test_filter_validation():
    with pytest.raises(ValueError):
        CandidateFilter(min_length=8, max_length=4)
    with pytest.raises(re.error):
        CandidateFilter(regex="(")


def test_filter_as_dict_omits_unset():
    assert CandidateFilter(min_length=3).as_dict() == {"min_length": 3}


def test_iter_candidates_counts_and_streams(tmp_path):
    words = tmp_path / "words.txt"
    words.write_text(
        "﻿Glacier\n\n  lantern  \n# comment\nab\nO'Brien\nglacier\ncafé au lait\nvery-very-long-word-here\nbad word\n",
        encoding="utf-8",
    )
    stats = ReadStats()
    candidates = list(iter_candidates(words, CandidateOptions(transform=Transform.COMPACT), stats=stats))
    assert [c.username for c in candidates] == ["glacier", "lantern", "obrien", "glacier", "cafeaulait", "badword"]
    assert candidates[0].line_no == 1
    assert candidates[0].source_word == "Glacier"
    assert (stats.lines, stats.blank, stats.invalid, stats.accepted) == (10, 2, 2, 6)
    assert [c.username for c in dedupe(candidates)] == ["glacier", "lantern", "obrien", "cafeaulait", "badword"]


def test_lowercase_mode_keeps_spaces_so_validator_rejects(tmp_path):
    words = tmp_path / "w.txt"
    words.write_text("bad word\nfine\n", encoding="utf-8")
    assert [c.username for c in iter_candidates(words, CandidateOptions())] == ["fine"]


def test_prefix_suffix_and_filters_apply_to_final_name(tmp_path):
    words = tmp_path / "w.txt"
    words.write_text("fox\nwolf\nbear\n", encoding="utf-8")
    options = CandidateOptions(prefix="x", suffix="s", filter=CandidateFilter(max_length=5))
    assert [c.username for c in iter_candidates(words, options)] == ["xfoxs"]


def test_iter_candidates_is_lazy(tmp_path):
    words = tmp_path / "big.txt"
    words.write_text("\n".join(f"word{i:06d}" for i in range(50_000)), encoding="utf-8")
    stats = ReadStats()
    first = list(itertools.islice(iter_candidates(words, CandidateOptions(), stats=stats), 3))
    assert len(first) == 3
    assert stats.lines == 3  # nothing beyond what was consumed has been read


def test_undecodable_bytes_are_replaced(tmp_path):
    words = tmp_path / "w.txt"
    words.write_bytes(b"abc\xff\ndef\n")
    assert [c.username for c in iter_candidates(words, CandidateOptions())] == ["def"]


def test_fingerprint(tmp_path):
    words = tmp_path / "w.txt"
    words.write_text("fox\n", encoding="utf-8")
    base = wordlist_fingerprint(words, CandidateOptions())
    assert base == wordlist_fingerprint(words, CandidateOptions())
    assert base != wordlist_fingerprint(words, CandidateOptions(prefix="x"))
    words.write_text("fox\nwolf\n", encoding="utf-8")
    assert base != wordlist_fingerprint(words, CandidateOptions())


def test_is_dictionary_word():
    assert is_dictionary_word("glacier", "Glacier")
    assert is_dictionary_word("obrien", "O'Brien")
    assert not is_dictionary_word("xglacier", "glacier")
    assert not is_dictionary_word("glacier", None)


def test_candidate_dataclass():
    c = Candidate("abc", "abc", 1)
    assert c.username == "abc"
