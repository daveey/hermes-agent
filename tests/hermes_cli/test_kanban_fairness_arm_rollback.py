"""A refused `capture arm` must not leave its plan.json behind (or clobber the previous one)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_fairness_budget as budget
from hermes_cli import kanban_fairness_journal as journal
from scripts import kanban_fairness as cli
from scripts.kanban_fairness import PLAN, main


@pytest.fixture
def db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return str(kb.kanban_db_path())


def _arm(db, out, capture_id, *extra):
    return main(["capture", "arm", "--out", str(out), "--capture-id", capture_id, "--db", db, *extra])


def _journal_state(db):
    with kbc.connect_closing(Path(db)) as conn:
        return journal.status(conn)


def test_refused_arm_removes_the_new_plan_and_keeps_existing_journal(db, tmp_path, capsys):
    first = tmp_path / "first"
    assert _arm(db, first, "one") == 0
    before = _journal_state(db)
    second = tmp_path / "second"
    # Budget initialization intentionally precedes journal admission.
    budget.initialize(second)
    contents = _directory_bytes(second)
    assert _arm(db, second, "two") == 3
    assert "JournalError" in capsys.readouterr().err
    assert not (second / PLAN).exists()
    assert _directory_bytes(second) == contents
    assert _journal_state(db) == before
    assert json.loads((first / PLAN).read_text())["capture_id"] == "one"


def _directory_bytes(out):
    return {path.relative_to(out): path.read_bytes() for path in out.rglob("*") if path.is_file()}


def test_connect_failure_removes_plan_and_keeps_journal(db, tmp_path, monkeypatch):
    out = tmp_path / "out"
    before = _journal_state(db)
    budget.initialize(out)
    contents = _directory_bytes(out)

    def fail_connect(db):
        raise OSError("connection failed")

    monkeypatch.setattr(cli, "_connect", fail_connect)
    assert _arm(db, out, "one") == 3
    assert not (out / PLAN).exists()
    assert _directory_bytes(out) == contents
    assert _journal_state(db) == before


def test_drain_failure_preserves_successfully_armed_plan(db, tmp_path, monkeypatch):
    out = tmp_path / "out"

    def fail_drain(conn, out):
        raise OSError("drain failed")

    monkeypatch.setattr(cli.store, "drain", fail_drain)
    assert _arm(db, out, "one") == 3
    assert json.loads((out / PLAN).read_text())["capture_id"] == "one"
    assert _journal_state(db)["capture_id"] == "one"


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("replace", [False, True])
def test_post_commit_failure_keeps_committed_plan(db, tmp_path, monkeypatch, error, replace):
    out = tmp_path / "out"
    if replace:
        assert _arm(db, out, "one") == 0
        # Same capture id and clock: neither is a reliable commit witness.
        monkeypatch.setattr(journal, "_NOW_MS", str(_journal_state(db)["armed_ms"]))

    def fail_status(conn):
        raise error("after commit")

    monkeypatch.setattr(journal, "_open_status", fail_status)
    args = ("--replace", "--clock-error-s", "7") if replace else ()
    if error is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            _arm(db, out, "one", *args)
    else:
        assert _arm(db, out, "one", *args) == 3
    plan = json.loads((out / PLAN).read_text())
    assert plan["capture_id"] == _journal_state(db)["capture_id"] == "one"
    if replace:
        assert plan["assumptions"]["clock_error_s"] == 7


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_pre_commit_failure_restores_plan_and_journal(db, tmp_path, monkeypatch, error):
    out = tmp_path / "out"
    assert _arm(db, out, "one") == 0
    prior = (out / PLAN).read_bytes()
    before = _journal_state(db)

    def fail_triggers(conn):
        raise error("before commit")

    original_triggers = journal._triggers
    monkeypatch.setattr(journal, "_triggers", fail_triggers)
    if error is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            _arm(db, out, "two", "--replace")
    else:
        assert _arm(db, out, "two", "--replace") == 3
    monkeypatch.setattr(journal, "_triggers", original_triggers)
    assert (out / PLAN).read_bytes() == prior
    assert _journal_state(db) == before


def test_refused_replacement_restores_the_prior_plan(db, tmp_path):
    out = tmp_path / "out"
    assert _arm(db, out, "one") == 0
    prior = (out / PLAN).read_bytes()
    before = _journal_state(db)
    assert _arm(db, out, "bad id!", "--replace") == 3
    assert (out / PLAN).read_bytes() == prior
    assert _journal_state(db) == before
