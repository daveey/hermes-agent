"""A cron poll that overruns its timeout loses nothing it finished, and stops starting runs at its deadline."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scripts.ci import flaky_artifact as fa
from scripts.ci import flaky_kanban as fk
from tests.ci.flaky_artifact_fakes import FakeGitHub
from tests.ci.test_flaky_kanban import FakeBoard


class Killed(BaseException):
    """Stands in for the cron wrapper killing the poll mid-run."""


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(fk, "_gh", fake.gh)
    monkeypatch.setattr(fa, "download_zip", fake.download)
    for run_id, art_id in ((5, 100), (6, 101), (7, 102)):
        fake.simple(run_id=run_id, art_id=art_id)
    return fake


class _Clock:
    """``fk.time`` with a settable ``monotonic``; everything else is the real module."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name: str):
        return getattr(time, name)


def test_no_run_starts_after_the_deadline_and_the_rest_stay_unprocessed(gh, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(fk, "time", clock)
    state = fk.load_state(Path("/nonexistent/state.json"))

    def tick() -> None:  # each run takes 10 s
        clock.now += 10

    log = fk.poll_github("o/r", "tok", FakeBoard(), state, deadline=15.0, checkpoint=tick)
    assert log == ["FAILED poll_budget deferred=1", "POLL complete=2 empty=2 unavailable=0 not_applicable=0"]
    assert state["runs"] == ["5/1", "6/1"] and gh.downloads == [100, 101]
    assert fk.poll_github("o/r", "tok", FakeBoard(), state) == [
        "POLL complete=1 empty=1 unavailable=0 not_applicable=0"]
    assert state["runs"] == ["5/1", "6/1", "7/1"]


def test_main_saves_each_finished_run_before_a_kill(gh, monkeypatch, tmp_path):
    def download(repo, artifact_id, token):
        if artifact_id == 101:
            raise Killed
        return gh.download(repo, artifact_id, token)

    monkeypatch.setattr(fa, "download_zip", download)
    monkeypatch.setenv("GH_TOKEN", "tok")
    path = tmp_path / "state.json"
    assert fk.main(["--initialize-intake", "--state", str(path)]) == 0
    with pytest.raises(Killed):
        fk.main(["--from-github", "--repo", "o/r", "--state", str(path)])
    assert json.loads(path.read_text())["runs"] == ["7/1"]
    monkeypatch.setattr(fa, "download_zip", gh.download)
    gh.downloads.clear()
    assert fk.main(["--from-github", "--repo", "o/r", "--state", str(path)]) == 0
    # FIFO preserves discovery order, then rotates the interrupted run behind queued work.
    assert json.loads(path.read_text())["runs"] == ["7/1", "5/1", "6/1"] and gh.downloads == [100, 101]


def test_main_budget_defers_unstarted_runs_and_fails_the_cron(gh, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GH_TOKEN", "tok")
    path = tmp_path / "state.json"
    assert fk.main(["--initialize-intake", "--state", str(path)]) == 0
    assert fk.main(["--from-github", "--repo", "o/r", "--state", str(path), "--budget", "0"]) == 1
    out = capsys.readouterr().out
    assert "FAILED poll_budget deferred=3" in out and gh.downloads == []
    assert json.loads(path.read_text())["runs"] == []
