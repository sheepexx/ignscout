from datetime import timedelta
from pathlib import Path

import pytest

from minecraft_finder.config import AppConfig, ConfigError, load_config
from minecraft_finder.timeutil import parse_duration

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


def test_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config, warnings = load_config()
    assert config == AppConfig()
    assert warnings == []
    assert config.scanner.requests_per_second == 1.0  # conservative default


def test_example_config_is_valid():
    config, warnings = load_config(EXAMPLE)
    assert warnings == []
    assert config.scanner.workers == 4
    assert config.cache.ttl == timedelta(hours=24)
    assert config.providers.namemc.enabled is False


def test_values_unknown_keys_and_cwd_discovery(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        "[scanner]\nworkers = 2\nbogus = 1\n\n[cache]\nttl_hours = 6\n\n[output]\ndirectory = 'out'\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    config, warnings = load_config()
    assert config.scanner.workers == 2
    assert config.cache.ttl_hours == 6.0  # int coerced to float
    assert config.output.directory == "out"
    assert any("scanner.bogus" in w for w in warnings)


def test_credentials_are_refused(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[providers.minecraft]\naccess_token = "abc"\n', encoding="utf-8")
    config, warnings = load_config(path)
    assert any("never store credentials" in w for w in warnings)
    assert not hasattr(config.providers.minecraft, "access_token")


@pytest.mark.parametrize(
    "content",
    ['[scanner]\nworkers = "many"\n', "[scanner]\nrequests_per_second = 0\n", "[scanner]\nworkers = true\n", "scanner = 3\n", "[scanner\n"],
)
def test_invalid_configs(tmp_path, content):
    path = tmp_path / "c.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_explicit_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("24h", timedelta(hours=24)),
        ("30m", timedelta(minutes=30)),
        ("7d", timedelta(days=7)),
        ("1w", timedelta(weeks=1)),
        ("1h30m", timedelta(minutes=90)),
        ("90s", timedelta(seconds=90)),
        ("12", timedelta(hours=12)),
        ("0", timedelta(0)),
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "5x", "-1", "1h banana"])
def test_parse_duration_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_duration(text)
