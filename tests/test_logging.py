import io
import logging

from rich.console import Console

from minecraft_finder.logging_setup import redact, setup_logging


def test_redacts_bearer_tokens():
    assert "abc.def-ghi" not in redact("Authorization: Bearer abc.def-ghi")


def test_redacts_jwts_anywhere():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVl"
    assert jwt not in redact(f"token was {jwt} oops")


def test_redacts_key_value_secrets():
    assert redact("access_token=xyz123&foo=1") == "access_token=[REDACTED]&foo=1"
    assert "s3cret" not in redact('{"password": "s3cret"}')
    assert "abc" not in redact("Cookie: session=abc")


def test_secrets_never_reach_the_log_file(tmp_path):
    console = Console(file=io.StringIO())
    log_path = setup_logging(tmp_path, console=console)
    logging.getLogger("minecraft_finder.test").warning("request failed with %s", "Bearer supersecretvalue")
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = log_path.read_text(encoding="utf-8")
    assert "request failed" in content
    assert "supersecretvalue" not in content
    assert "supersecretvalue" not in console.file.getvalue()
