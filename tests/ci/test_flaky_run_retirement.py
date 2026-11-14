"""Journal mode retires a permanently unavailable run once GitHub no longer lists it.

Without the journal a run drops out of ``poll_github`` when it leaves the lookback
listing. The journal replays stored runs, so it needs an explicit, single terminal
FAILED line instead of logging FAILED on every poll forever.
"""
from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest

from scripts.ci import flaky_artifact as fa
from scripts.ci import flaky_intake as intake
from scripts.ci import flaky_kanban as fk
from tests.ci.flaky_artifact_fakes import FakeGitHub, artifact
from tests.ci.test_flaky_kanban import FakeBoard


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(fk, "_gh", fake.gh)
    monkeypatch.setattr(fa, "download_zip", fake.download)
    return fake


def _setup(gh, tmp_path):
    gh.simple(tests=("tests/x.py::test_a",))
    gh.artifacts[5] = [artifact(100, expired=True)]
    journal = intake.Journal(tmp_path / "queue.sqlite3")
    state = fk.load_state(Path("/nonexistent/state.json"))
    board = FakeBoard()
    return journal, state, board


def _poll(board, state, journal):
    return fk.poll_github("o/r", "tok", board, state, grace=0, journal=journal)


def _pending(journal):
    return [r[0] for r in journal.db.execute("SELECT key FROM runs WHERE complete=0")]


def test_unlisted_permanently_unavailable_run_is_retired_once(gh, tmp_path):
    journal, state, board = _setup(gh, tmp_path)
    assert _poll(board, state, journal)[0] == "WAITING run=5/1 code=expired polls=1"
    assert _poll(board, state, journal)[0] == "FAILED run=5/1 code=expired polls=2"
    gh.runs.clear()                                   # left the lookback listing
    log = _poll(board, state, journal)
    assert log[0] == "FAILED run=5/1 code=expired polls=3 retired=unlisted"
    assert _pending(journal) == []
    assert _poll(board, state, journal) == []         # silent afterwards
    assert _poll(board, state, journal) == []
    assert not board.cards


def test_listing_failure_never_retires(gh, tmp_path):
    journal, state, board = _setup(gh, tmp_path)
    _poll(board, state, journal)
    _poll(board, state, journal)
    gh.fail["actions/runs?"] = urllib.error.URLError("down")
    log = _poll(board, state, journal)
    assert "FAILED runs_unavailable" in log
    assert "FAILED run=5/1 code=expired polls=3" in log
    assert not any("retired" in line for line in log)
    assert _pending(journal) == ["5/1"]


def test_still_listed_run_stays_failed_and_unretired(gh, tmp_path):
    journal, state, board = _setup(gh, tmp_path)
    for polls in (1, 2, 3, 4):
        log = _poll(board, state, journal)
    assert log[0] == "FAILED run=5/1 code=expired polls=4"
    assert _pending(journal) == ["5/1"]


def test_unlisted_run_on_first_failure_waits_before_retiring(gh, tmp_path):
    journal, state, board = _setup(gh, tmp_path)
    _poll(board, state, journal)
    gh.runs.clear()
    assert _poll(board, state, journal)[0] == "FAILED run=5/1 code=expired polls=2 retired=unlisted"
    assert _pending(journal) == []


def test_unlisted_run_that_recovers_is_still_processed(gh, tmp_path):
    journal, state, board = _setup(gh, tmp_path)
    gh.artifacts[5] = [artifact(100)]
    gh.fail["/artifacts"] = urllib.error.URLError("down")
    _poll(board, state, journal)
    gh.runs.clear()
    gh.fail.clear()
    log = _poll(board, state, journal)
    assert not any(line.startswith("FAILED") for line in log)
    assert _pending(journal) == []
    assert journal.db.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0] == 1
