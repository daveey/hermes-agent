"""Daily intake delivery through the REAL `hermes kanban` CLI (isolated HERMES_HOME), not an in-memory Board.

The in-memory fakes returned ``idempotency_key`` from card/list readback; the real CLI JSON did not, so every
production delivery failed its identity readback ("FAILED intake delivery page=1") after creating the card.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci import flaky_intake as intake
from scripts.ci import flaky_kanban as fk

_ROOT = Path(__file__).resolve().parents[2]
REPO = "o/r"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    pytest.importorskip("dotenv", reason="needs the repo environment (hermes_cli imports python-dotenv)")
    home = tmp_path / "hermes-home"
    home.mkdir()
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_DELEGATED_CHILD_OWNER",
                "HERMES_SESSION_ID", "HERMES_SESSION_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.chdir(_ROOT)

    def run(*args):
        return subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", *args], capture_output=True,
                              text=True, timeout=120, check=True, cwd=_ROOT).stdout
    return run


def _record(journal, run, tests):
    journal.record(REPO, [(9, [{"test_id": t, "retried": "tests"} for t in tests])],
                   {"id": run, "head_sha": "a" * 40})
    journal.plan_pages(REPO, journal.pending(REPO))


@pytest.mark.platforms("posix")
def test_kanban_json_exposes_idempotency_key(cli):
    tid = json.loads(cli("create", "keyed", "--idempotency-key", "k:1", "--json"))["id"]
    assert json.loads(cli("show", tid, "--json"))["task"]["idempotency_key"] == "k:1"
    assert [c["idempotency_key"] for c in json.loads(cli("list", "--archived", "--json"))] == ["k:1"]
    board = fk.CliBoard([sys.executable, "-m", "hermes_cli.main"])
    assert board.card(tid)["idempotency_key"] == "k:1"


@pytest.mark.platforms("posix")
def test_daily_intake_delivers_through_real_cli_and_reopens_done(cli, tmp_path):
    board = fk.CliBoard([sys.executable, "-m", "hermes_cli.main"])
    journal = intake.Journal(tmp_path / "queue.sqlite3")
    tests = [f"tests/a.py::test_{i}" for i in range(30)]
    _record(journal, 1, tests)

    log = journal.deliver(REPO, board)
    assert log and not any(line.startswith("FAILED") for line in log), log
    assert tuple(journal.db.execute("SELECT count(*), sum(delivered) FROM occurrences").fetchone()) == (30, 30)
    cards = json.loads(cli("list", "--archived", "--json"))
    day = journal.db.execute("SELECT day FROM pages").fetchone()[0]
    assert [c["idempotency_key"] for c in cards] == [f"ci-flaky-intake:{REPO}:{day}"]
    card = cards[0]["id"]
    payloads = [r[0] for r in journal.db.execute("SELECT payload FROM pages ORDER BY id")]
    comments = [c["body"] for c in json.loads(cli("show", card, "--json"))["comments"]]
    assert comments == payloads
    assert journal.deliver(REPO, board) == []  # replay: no duplicate card or comment

    # A triaged (done) intake must not swallow a later same-day occurrence: it is reopened and receives it.
    cli("complete", card, "--summary", "triaged")
    _record(journal, 2, ["tests/b.py::test_late"])
    log = journal.deliver(REPO, board)
    assert not any(line.startswith("FAILED") for line in log), log
    assert tuple(journal.db.execute("SELECT count(*), sum(delivered) FROM occurrences").fetchone()) == (31, 31)
    assert len(json.loads(cli("list", "--archived", "--json"))) == 1
    shown = json.loads(cli("show", card, "--json"))
    assert shown["task"]["status"] != "done"
    bodies = [c["body"] for c in shown["comments"]]
    late = journal.db.execute("SELECT payload FROM pages ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert bodies[:len(payloads)] == payloads and bodies.count(late) == 1 and late not in payloads
    journal.close()
