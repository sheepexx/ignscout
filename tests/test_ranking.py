from minecraft_finder.ranking import (
    DICTIONARY_POINTS,
    pronounceability,
    quality_score,
    score_breakdown,
)


def test_shorter_names_score_higher():
    assert quality_score("fox") > quality_score("foxes") > quality_score("foxesandwolves")


def test_digits_and_underscores_are_penalised():
    assert quality_score("glacier") > quality_score("glac1er") > quality_score("gl4c1er")
    assert quality_score("glacier") > quality_score("gla_cier")


def test_dictionary_bonus():
    assert quality_score("glacier", dictionary_word=True) - quality_score("glacier") == DICTIONARY_POINTS


def test_pronounceable_beats_consonant_soup():
    assert quality_score("velvet") > quality_score("xkcdqz")
    assert pronounceability("hello") > pronounceability("helllo")
    assert pronounceability("1234") == 0.0


def test_scores_are_bounded():
    for name in ("abc", "zzz", "___", "a1_", "velvet", "x" * 16, "aeiouaeiouaeiou"):
        breakdown = score_breakdown(name, dictionary_word=True)
        assert 0 <= breakdown.total <= 100


def test_dictionary_bonus_requires_letters_only():
    assert score_breakdown("echo1", dictionary_word=True).dictionary == 0
    assert quality_score("lantern", dictionary_word=True) > quality_score("echo1", dictionary_word=True)
