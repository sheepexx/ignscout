"""Confirming "likely available" names with Mojang's token-backed check (mocked HTTP)."""

import httpx
import pytest
from typer.testing import CliRunner

from minecraft_finder import cli
from minecraft_finder.cli import app, clean_token
from minecraft_finder.database import Database
from minecraft_finder.models import CheckResult, Confidence, Status
from minecraft_finder.providers.minecraft import MinecraftProvider

from .conftest import mock_client

runner = CliRunner()
TOKEN = "eyJhbGciOiJIUzI1NiJ9.dGVzdC10b2tlbi1wYXlsb2Fk.c2lnbmF0dXJlLXZhbHVl"
ANSWERS = {"glacier": "AVAILABLE", "bassi": "DUPLICATE", "refer": "DUPLICATE", "die": "NOT_ALLOWED", "zzzlow": "AVAILABLE"}


def handler(seen: list[str]):
    def respond(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        if request.url.path.endswith("/available"):
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            name = parts[-2]
            seen.append(name)
            return httpx.Response(200, json={"status": ANSWERS[name]})
        name = parts[-1]
        if name == "refer":  # claimed by someone since the scan
            return httpx.Response(200, json={"id": "a" * 32, "name": "Refer"})
        return httpx.Response(404, json={"path": request.url.path, "errorMessage": f"Couldn't find any profile with name {name}"})

    return respond


@pytest.fixture
def workdir(tmp_path, monkeypatch, clock):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINECRAFT_ACCESS_TOKEN", TOKEN)
    with Database(tmp_path / "data" / "results.db") as db:
        db.upsert_results(
            [
                CheckResult(name, Status.AVAILABLE, "minecraft", confidence=Confidence.UNVERIFIED, quality_score=score)
                for name, score in (("glacier", 90), ("bassi", 80), ("refer", 70), ("die", 60), ("zzzlow", 10))
            ]
        )
        db.commit()
    seen: list[str] = []

    def factory(config, scanner, *, token=None, verify=False):
        return MinecraftProvider(
            config, scanner, client=mock_client(handler(seen)), token=token, verify=verify, sleep=clock.sleep, clock=clock
        )

    monkeypatch.setattr(cli, "MinecraftProvider", factory)
    return tmp_path, seen


def test_verify_sorts_names_into_free_soon_taken_blocked(workdir):
    path, seen = workdir
    result = runner.invoke(app, ["verify", "--top", "4"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert seen == ["glacier", "bassi", "refer", "die"]  # best score first; "zzzlow" not checked
    assert "Confirmed with Mojang" in result.output
    with Database(path / "data" / "results.db") as db:
        glacier, bassi, refer, die, low = (db.get_result(n) for n in ("glacier", "bassi", "refer", "die", "zzzlow"))
    assert (glacier.status, glacier.confidence) == (Status.AVAILABLE, Confidence.CONFIRMED)
    assert bassi.status is Status.SOON and bassi.available_at is None  # on hold, date unknown
    assert refer.status is Status.TAKEN
    assert (die.status, die.confidence) == (Status.BLOCKED, Confidence.CONFIRMED)
    assert low.confidence is Confidence.UNVERIFIED
    assert (path / "output" / "confirmed.txt").read_text(encoding="utf-8").split() == ["glacier"]
    assert glacier.quality_score == 90  # score kept for sorting

    again = runner.invoke(app, ["verify", "--top", "4"], catch_exceptions=False)
    assert seen[4:] == ["zzzlow"]  # already-confirmed names are not checked twice
    assert again.exit_code == 0


def test_verify_max_length_checks_only_short_names(workdir):
    _, seen = workdir
    result = runner.invoke(app, ["verify", "--max-length", "5"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert seen == ["bassi", "refer", "die"]  # glacier (7) and zzzlow (6) are too long
    assert "On hold or locked" in result.output


def test_menu_can_confirm_only_short_names(workdir):
    _, seen = workdir
    answers = ["6", "2", "1", "n", "", "0"]  # confirm · only short names · best 25 · don't open the folder
    result = runner.invoke(app, [], input="\n".join(answers) + "\n", catch_exceptions=False)
    assert "Only short names" in result.output
    assert "names of up to 4 characters are waiting" in result.output
    assert seen == ["die"]  # the only name with 3 or 4 characters


def test_rejected_token_stops_and_changes_nothing(workdir, monkeypatch):
    path, _ = workdir

    def factory(config, scanner, *, token=None, verify=False):
        return MinecraftProvider(
            config, scanner, client=mock_client(lambda r: httpx.Response(401, json={"path": r.url.path})), token=token, verify=verify
        )

    monkeypatch.setattr(cli, "MinecraftProvider", factory)
    result = runner.invoke(app, ["verify"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "rejected the token" in result.output
    with Database(path / "data" / "results.db") as db:
        assert db.get_result("glacier").confidence is Confidence.UNVERIFIED


def test_export_lists_confirmed_names_first(workdir):
    path, _ = workdir
    runner.invoke(app, ["verify", "--top", "4"], catch_exceptions=False)
    with Database(path / "data" / "results.db") as db:
        db.upsert_result(CheckResult("zzzbest", Status.AVAILABLE, "minecraft", confidence=Confidence.UNVERIFIED, quality_score=99))
        db.commit()
    names = runner.invoke(app, ["export", "--format", "txt", "-o", "-"]).output.split()
    assert names[0] == "glacier"  # confirmed beats a higher-scoring unconfirmed name
    assert names.index("glacier") < names.index("zzzbest")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(f"Bearer {TOKEN}", TOKEN), (f'  "{TOKEN}"  ', TOKEN), (TOKEN, TOKEN), ("   ", None), ("bearer   ", None)],
)
def test_clean_token(raw, expected):
    assert clean_token(raw) == expected


def test_token_is_never_written_anywhere(workdir):
    path, _ = workdir
    runner.invoke(app, ["--debug", "verify", "--top", "4"], catch_exceptions=False)
    for file in [*path.rglob("*.log"), *path.rglob("*.txt"), *path.rglob("*.jsonl")]:
        assert TOKEN not in file.read_text(encoding="utf-8", errors="replace"), file
