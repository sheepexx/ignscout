"""Minecraft: Java Edition username rules — the single source of truth.

Rules for names that can be claimed today (new profiles and profile-name changes):

* 3 to 16 characters
* only ASCII letters ``A-Z`` / ``a-z``, digits ``0-9`` and underscore ``_``

A few legacy accounts still own names that break these rules (for example
two-character names). Those names cannot be claimed, so they are rejected here
before any network request is made.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

MIN_LENGTH = 3
MAX_LENGTH = 16
ALLOWED_DESCRIPTION = "A-Z, a-z, 0-9 and _"

# Explicit ASCII class: ``\w`` would also accept Unicode letters and digits.
_VALID_RE = re.compile(rf"[A-Za-z0-9_]{{{MIN_LENGTH},{MAX_LENGTH}}}")
_INVALID_CHAR_RE = re.compile(r"[^A-Za-z0-9_]")


class InvalidReason(enum.StrEnum):
    EMPTY = "empty"
    WHITESPACE = "whitespace"
    INVALID_CHARACTERS = "invalid_characters"
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    username: str
    reason: InvalidReason | None = None
    message: str = ""

    @property
    def valid(self) -> bool:
        return self.reason is None

    def __bool__(self) -> bool:
        return self.valid


def is_valid_username(name: str) -> bool:
    """Fast check used on the hot path while streaming word lists."""
    return _VALID_RE.fullmatch(name) is not None


def validate_username(name: str) -> ValidationResult:
    """Validate ``name`` and explain the first rule it breaks."""
    if is_valid_username(name):
        return ValidationResult(name)
    if not name:
        return ValidationResult(name, InvalidReason.EMPTY, "username is empty")
    if any(ch.isspace() for ch in name):
        return ValidationResult(name, InvalidReason.WHITESPACE, "username contains whitespace")
    bad = sorted(set(_INVALID_CHAR_RE.findall(name)))
    if bad:
        shown = " ".join(repr(ch) for ch in bad[:5])
        return ValidationResult(
            name,
            InvalidReason.INVALID_CHARACTERS,
            f"unsupported characters {shown} (allowed: {ALLOWED_DESCRIPTION})",
        )
    if len(name) < MIN_LENGTH:
        return ValidationResult(
            name, InvalidReason.TOO_SHORT, f"too short ({len(name)} < {MIN_LENGTH} characters)"
        )
    return ValidationResult(
        name, InvalidReason.TOO_LONG, f"too long ({len(name)} > {MAX_LENGTH} characters)"
    )
