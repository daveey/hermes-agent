"""Online worker copies may be evicted; the Board replica retains their status."""
import sqlite3
import subprocess

import pytest

from hermes_cli import kanban_awaiting as awaiting
from hermes_cli import kanban_wait_check as wait

TID = "t_123456789abcdef0"


@pytest.fixture
def boards(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    for path in (tmp_path / "kanban.db", tmp_path / "kanban/sync-shadow/default.db"):
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, status TEXT)")
    return tmp_path


def put(home, status, *, live=False):
    path = home / ("kanban.db" if live else "kanban/sync-shadow/default.db")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO tasks VALUES (?, ?)", (TID, status))


def remote_on_fixture(monkeypatch):
    def ssh(argv, **kwargs):
        rc, text = wait.check_local("task", TID, board="default", until=("done",))
        return subprocess.CompletedProcess(argv, rc, text, "")
    monkeypatch.setattr(wait.subprocess, "run", ssh)


@pytest.mark.parametrize("status,rc", [("done", 0), ("running", 1), (None, 2)])
@pytest.mark.parametrize("remote", [False, True], ids=["local", "remote"])
def test_evicted_copy(boards, monkeypatch, status, rc, remote):
    if status:
        put(boards, status)
    if remote:
        remote_on_fixture(monkeypatch)
        result = wait.check_remote("hub", "task", TID, board="default", until=("done",))
    else:
        result = wait.check_local("task", TID, until=("done",))
    assert result[0] == rc, result
    if status:
        assert result[1].split()[:2] == [TID, status]


@pytest.mark.parametrize("status", ["done", "running", None])
def test_awaiting_validation_uses_board_status(boards, monkeypatch, status):
    if status:
        put(boards, status)
    remote_on_fixture(monkeypatch)
    monkeypatch.setattr(awaiting, "_REMOTE_CONFIRMED", {})
    record = dict(kind="card", ref=TID, gateway="hub", board="default")
    if status == "running":
        awaiting.confirm_remote(record)
        assert ("hub", TID) in awaiting._REMOTE_CONFIRMED
    else:
        with pytest.raises(awaiting.AwaitingError) as error:
            awaiting.confirm_remote(record)
        assert error.value.code == (awaiting.PRODUCER_DONE if status == "done" else awaiting.INVALID_AWAITING)


def test_live_precedence_and_since(boards):
    put(boards, "done")
    assert wait.check_local("task", TID, since=("done",))[0] == 1
    put(boards, "running", live=True)
    assert wait.check_local("task", TID, until=("done",))[0] == 1


def test_missing_live_file_still_resolves(boards):
    put(boards, "done")
    (boards / "kanban.db").unlink()
    assert wait.check_local("task", TID, until=("done",))[0] == 0


def test_named_board_is_not_default(boards):
    put(boards, "done")
    path = boards / "kanban/sync-shadow/other.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, status TEXT)")
        db.execute("INSERT INTO tasks VALUES (?, 'running')", (TID,))
    assert wait.check_local("task", TID, board="other", until=("done",))[0] == 1


def test_profile_home_uses_canonical_board_shadow(boards, monkeypatch):
    put(boards, "done")
    monkeypatch.setenv("HERMES_HOME", str(boards / "profiles" / "other"))
    assert wait.check_local("task", TID, until=("done",))[0] == 0


def test_corrupt_live_database_is_not_hidden(boards):
    put(boards, "done")
    (boards / "kanban.db").write_text("not sqlite")
    assert wait.check_local("task", TID, until=("done",))[0] == 2
