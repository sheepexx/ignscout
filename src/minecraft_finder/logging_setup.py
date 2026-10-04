"""Logging: a detailed rotating log file plus a quiet console, both with secret redaction."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

LOG_FILE_NAME = "minecraft-finder.log"
_MARKER = "_minecraft_finder_handler"

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)((?:access[_-]?token|authorization|cookie|set-cookie|api[_-]?key|password|secret)"
            r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&]+"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED-JWT]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Scrubs bearer tokens, JWTs, cookies and similar values from every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed record; let logging report it
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        return True


def setup_logging(
    log_dir: Path, *, verbose: bool = False, debug: bool = False, console: Console | None = None
) -> Path:
    """Configure logging and return the log file path. Safe to call repeatedly."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_FILE_NAME

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _MARKER, False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(logging.DEBUG)
    redactor = RedactingFilter()

    file_handler = RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8", delay=True
    )
    file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    file_handler.addFilter(redactor)
    setattr(file_handler, _MARKER, True)
    root.addHandler(file_handler)

    console_handler = RichHandler(
        console=console,
        show_time=debug,
        show_path=False,
        markup=False,
        rich_tracebacks=debug,
    )
    console_handler.setLevel(
        logging.DEBUG if debug else logging.INFO if verbose else logging.WARNING
    )
    console_handler.addFilter(redactor)
    setattr(console_handler, _MARKER, True)
    root.addHandler(console_handler)

    # httpx logs one INFO line per request; keep that for --debug only. httpcore stays quiet.
    logging.getLogger("httpx").setLevel(logging.INFO if debug else logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return log_path
