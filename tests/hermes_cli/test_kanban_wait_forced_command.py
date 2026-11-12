"""Restricted SSH is transport unavailability, not a failed owner observation."""
import json
import subprocess

import pytest

from hermes_cli import kanban_wait_check as wait
from hermes_cli import kanban_awaiting as awaiting
from tests.hermes_cli import test_kanban_wait_replica as fixtures

replica = fixtures.replica
board = fixtures.board
TID = fixtures.TID
REFUSAL = "hub-read: refused: 'export PATH=...; hermes kanban wait-check ...' (run `help`)"


@pytest.fixture
def restricted(replica, monkeypatch):
    calls = []

    def ssh(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 2, "", REFUSAL)

    monkeypatch.setattr(wait.subprocess, "run", ssh)
    return replica, calls


@pytest.mark.parametrize("status,rc", [("ready", 1), ("done", 0), ("scheduled", 1)])
def test_refusal_uses_owner_qualified_replica(restricted, status, rc):
    db, calls = restricted
    fixtures.put(db, status)
    code, text = wait.check_remote("other", "task", TID)
    assert code == rc, text
    assert text == f"{TID} {status} (replica)"
    assert len(calls) == 1  # no retry using an unrestricted key or alternate shell
    assert calls[0][-2] == "other"
    assert wait.check_remote("other", "task", TID, since=(status,))[0] == 1


def test_refusal_keeps_stale_missing_and_transport_distinct(restricted, monkeypatch):
    db, _ = restricted
    from hermes_cli import kanban_wait_live

    def no_live(*args, **kwargs):
        raise ValueError("bounded live transport unavailable")

    monkeypatch.setattr(kanban_wait_live, "read_task", no_live)
    rc, out = wait.check_remote("other", "task", TID)
    assert rc == 2 and "transport unavailable" in out
    fixtures.put(db, "done")
    fixtures.make_stale(db, 1237)
    rc, out = wait.check_remote("other", "task", TID)
    assert rc == 3 and "replica stale" in out and "gateway=other" in out
    # A matching id on a different board/host must not satisfy this wait.
    assert wait.check_remote("other", "task", TID, board="foreign")[0] == 2
    assert wait.check_remote("foreign", "task", TID)[0] == 2


@pytest.mark.parametrize("rc,stderr,kind", [
    (2, "error: database is corrupt", "task"),
    (1, REFUSAL, "task"),
    (2, "unrelated output: " + REFUSAL, "task"),
    (2, REFUSAL, "board-empty"),
])
def test_only_explicit_forced_command_refusal_falls_back(replica, monkeypatch, rc, stderr, kind):
    fixtures.put(replica, "done")
    monkeypatch.setattr(wait.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, rc, "", stderr))
    actual, text = wait.check_remote("other", kind, TID)
    assert actual == (1 if rc == 1 else 2)
    assert "(replica)" not in text


def test_refusal_confirm_and_lifecycle_park(restricted, board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    db, _ = restricted
    conn, tid = board
    fixtures.put(db, "ready")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb.get_task(conn, tid).current_run_id))
    result = json.loads(kt._handle_schedule({
        "reason": f"SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on {TID} on other",
        "wake_check": "exit 1", "check_every": 900,
        "awaiting": {"kind": "card", "ref": TID, "gateway": "other"},
    }))
    assert result.get("status") == "scheduled", result
    assert kb.get_task(conn, tid).status == "scheduled"
    assert fixtures.parked_observation(kb.list_events(conn, tid)) == f"{TID} ready (replica)"
    db.execute("DELETE FROM replica WHERE coll='tasks'")
    fixtures.put(db, "archived")
    with pytest.raises(awaiting.AwaitingError, match="producer card is not live"):
        awaiting.confirm_remote({"kind": "card", "ref": TID, "gateway": "other"})
