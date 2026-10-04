"""CLI commands, driven offline through the demo provider (no network)."""

import json

import pytest
from typer.testing import CliRunner

from minecraft_finder.cli import app

runner = CliRunner()
WORDS = [f"{a}{b}name" for a in "abcdefgh" for b in "xyz"] + ["Glacier", "glacier", "ab", "bad word"]


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINECRAFT_ACCESS_TOKEN", raising=False)
    (tmp_path / "config.toml").write_text(
        "[scanner]\nrequests_per_second = 50\n\n[providers.demo]\nlatency = 0.0\nerror_rate = 0.0\n", encoding="utf-8"
    )
    (tmp_path / "words.txt").write_text("\n".join(WORDS) + "\n", encoding="utf-8")
    return tmp_path


def invoke(*args):
    return runner.invoke(app, list(args), catch_exceptions=False)


def test_help_lists_commands(workdir):
    result = invoke("--help")
    assert result.exit_code == 0
    for command in ("scan", "check", "stats", "export", "providers", "database"):
        assert command in result.output


def test_check_rejects_invalid_names_without_network(workdir):
    result = invoke("check", "ab", "bad-name", "--provider", "demo")
    assert result.exit_code == 0
    assert result.output.count("INVALID") == 2
    assert "too short" in result.output
    assert not (workdir / "data").exists()  # nothing was even opened


def test_check_uses_cache_on_second_call(workdir):
    first = invoke("check", "glacier", "--provider", "demo")
    assert first.exit_code == 0
    assert "glacier" in first.output
    assert (workdir / "data" / "demo-results.db").exists()
    second = invoke("check", "glacier", "--provider", "demo")
    assert "(cached)" in second.output
    forced = invoke("check", "glacier", "--provider", "demo", "--force")
    assert "(cached)" not in forced.output


def test_check_json_output(workdir):
    result = invoke("check", "glacier", "lantern", "--provider", "demo", "--json")
    records = [json.loads(line) for line in result.output.strip().splitlines()]
    assert [r["username"] for r in records] == ["glacier", "lantern"]
    assert all(r["status"] in {"available", "soon", "taken", "blocked"} for r in records)


def test_scan_end_to_end_with_resume_and_cache(workdir):
    result = invoke("scan", "words.txt", "--provider", "demo", "--workers", "4")
    assert result.exit_code == 0, result.output
    assert "Words loaded" in result.output
    assert "25" in result.output  # 24 generated + Glacier (deduplicated); 'ab' and 'bad word' invalid
    assert "Scan complete" in result.output
    jsonl = (workdir / "output" / "demo" / "results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(jsonl) == 25

    again = invoke("scan", "words.txt", "--provider", "demo")
    assert "already complete" in again.output

    restarted = invoke("scan", "words.txt", "--provider", "demo", "--restart")
    assert restarted.exit_code == 0
    assert "25 from cache" in restarted.output
    assert len((workdir / "output" / "demo" / "results.jsonl").read_text(encoding="utf-8").splitlines()) == 25


def test_scan_filters_and_dry_run(workdir):
    result = invoke("scan", "words.txt", "--provider", "demo", "--starts-with", "a", "--dry-run")
    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "3" in result.output
    assert not (workdir / "output" / "demo" / "results.jsonl").exists()


def test_scan_rejects_bad_regex_and_missing_token(workdir):
    bad = runner.invoke(app, ["scan", "words.txt", "--regex", "("])
    assert bad.exit_code == 2
    assert "invalid regular expression" in bad.output
    no_token = runner.invoke(app, ["scan", "words.txt", "--verify"])
    assert no_token.exit_code == 2
    assert "access token" in no_token.output


def test_stats_export_and_database_commands(workdir):
    assert invoke("scan", "words.txt", "--provider", "demo").exit_code == 0

    stats = invoke("stats", "--demo")
    assert stats.exit_code == 0
    assert "Results database" in stats.output
    assert "Scan jobs" in stats.output

    exported = invoke("export", "--demo", "--status", "all", "--format", "jsonl")
    assert exported.exit_code == 0
    lines = (workdir / "output" / "demo" / "export.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 25
    scores = [json.loads(line)["quality_score"] for line in lines]
    assert scores == sorted(scores, reverse=True)  # --sort quality is the default

    to_stdout = invoke("export", "--demo", "--status", "taken", "--format", "txt", "--sort", "name", "-o", "-")
    names = to_stdout.output.split()
    assert names == sorted(names)

    info = invoke("database", "info", "--demo")
    assert info.exit_code == 0 and "Schema" in info.output
    clean = invoke("database", "clean", "--demo", "--yes")
    assert clean.exit_code == 0 and "Removed" in clean.output


def test_providers_command(workdir):
    result = invoke("providers")
    assert result.exit_code == 0
    for name in ("Minecraft", "NameMC", "Demo"):
        assert name in result.output


def test_config_secrets_are_refused(workdir):
    (workdir / "config.toml").write_text('[providers.minecraft]\naccess_token = "abc"\n', encoding="utf-8")
    result = invoke("providers")
    assert "never store credentials" in result.output


def test_generate_writes_every_short_name(workdir):
    result = invoke("generate", "3", "numbers")
    assert result.exit_code == 0, result.output
    names = (workdir / "wordlists" / "all-3-with-numbers.txt").read_text(encoding="utf-8").split()
    assert len(names) == 29_080 and names[0] == "aa0"
    assert "minecraft-finder scan" in result.output
    assert invoke("generate", "5").exit_code != 0  # only 3 or 4 characters
