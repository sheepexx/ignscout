import pytest

from minecraft_finder.validator import (
    MAX_LENGTH,
    MIN_LENGTH,
    InvalidReason,
    is_valid_username,
    validate_username,
)


@pytest.mark.parametrize("name", ["abc", "Notch", "jeb_", "a_b_c", "x" * 16, "ABC123", "___", "Player_2024"])
def test_valid_names(name):
    assert is_valid_username(name)
    result = validate_username(name)
    assert result.valid
    assert result.reason is None
    assert bool(result)


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("", InvalidReason.EMPTY),
        ("ab", InvalidReason.TOO_SHORT),
        ("x", InvalidReason.TOO_SHORT),
        ("x" * 17, InvalidReason.TOO_LONG),
        ("hello world", InvalidReason.WHITESPACE),
        (" abc", InvalidReason.WHITESPACE),
        ("abc\n", InvalidReason.WHITESPACE),
        ("café", InvalidReason.INVALID_CHARACTERS),
        ("ab-cd", InvalidReason.INVALID_CHARACTERS),
        ("dot.name", InvalidReason.INVALID_CHARACTERS),
        ("abc!", InvalidReason.INVALID_CHARACTERS),
        ("ａｂｃ", InvalidReason.INVALID_CHARACTERS),  # full-width letters
        ("abc١٢٣", InvalidReason.INVALID_CHARACTERS),  # Arabic-Indic digits match \d but are not allowed
        ("o'brien", InvalidReason.INVALID_CHARACTERS),
    ],
)
def test_invalid_names(name, reason):
    result = validate_username(name)
    assert not result.valid
    assert not result
    assert result.reason is reason
    assert result.message
    assert not is_valid_username(name)


def test_limits_are_minecraft_rules():
    assert (MIN_LENGTH, MAX_LENGTH) == (3, 16)


def test_messages_are_helpful():
    assert "3" in validate_username("ab").message
    assert "16" in validate_username("a" * 20).message
    assert "'-'" in validate_username("ab-cd").message
