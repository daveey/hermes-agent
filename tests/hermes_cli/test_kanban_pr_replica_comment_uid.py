"""Replica cards map a ``commented`` event to its row by ``comment_uid``, not by a drifting ``len``.

2026-11-20 t_01a1f772bf13a473: the dispatcher's wake note ended in a space, ``add_comment`` stored the
stripped body but recorded the unstripped length, and the replica row (new local id) could not be
matched to its event, so the note counted as a new PR publication and held the card ``active_pr``.
"""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_external_watch as kew
from tests.hermes_cli.test_kanban_park_invariants import kanban_home

__all__ = ["kanban_home"]
pytestmark = pytest.mark.usefixtures("kanban_home")
URL = "https://github.com/example/project/pull/3796"
OFFSET = 1000


@pytest.fixture(autouse=True)
def open_pr(monkeypatch):
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)


def _worker_comment(conn, tid, run_id, monkeypatch, body):
    with monkeypatch.context() as env:
        env.setenv("HERMES_KANBAN_TASK", tid)
        env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        return kb.add_comment(conn, tid, "a", body)


def _replicate_with_uid(conn, tid, cid, uid, len_delta):
    """Renumber comment ``cid`` as a replica does (new local id, portable uid) and leave its event
    naming the HOME id and uid, with ``len`` ``len_delta`` off the stored body."""
    if not any(c[1] == "uid" for c in conn.execute("PRAGMA table_info(task_comments)")):
        conn.execute("ALTER TABLE task_comments ADD COLUMN uid TEXT")
    conn.execute("UPDATE task_comments SET uid = ? WHERE id = ?", (uid, cid))
    conn.execute("UPDATE task_comments SET id = id + ? WHERE task_id = ?", (OFFSET, tid))
    for eid, payload in conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id=? AND kind='commented'", (tid,)).fetchall():
        data = json.loads(payload)
        if data.get("comment_id") == cid:
            data["comment_uid"] = uid
            data["len"] += len_delta
            conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(data), eid))


def _owner_parked(conn, monkeypatch, now):
    tid = kb.create_task(conn, title="finish owned PR", assignee="a")
    kb.claim_task(conn, tid)
    run_id = conn.execute("SELECT id FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
    _worker_comment(conn, tid, run_id, monkeypatch, f"Opened {URL}")
    monkeypatch.setattr(time, "time", lambda: now + 1)
    assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z CI", clock_only=True)
    monkeypatch.setattr(time, "time", lambda: now + 8)
    return tid, run_id


def test_replica_wake_note_with_stale_len_is_foreign_by_uid(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with kbc.connect() as conn:
        tid, _ = _owner_parked(conn, monkeypatch, now)
        cid = kb.add_comment(conn, tid, "kanban-dispatcher", f"Woken: the job finished: succeeded ({URL}). ")
        _replicate_with_uid(conn, tid, cid, "01a1f9cd-c774-709c-b90d-720d0241b4b4", len_delta=1)
        assert prs.active_pr_reason(conn, tid, now + 700) is None


def test_replica_wake_note_with_stale_len_holds_without_a_uid(monkeypatch):
    """The control: without the uid the legacy triple cannot map it, and that fails closed."""
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with kbc.connect() as conn:
        tid, _ = _owner_parked(conn, monkeypatch, now)
        cid = kb.add_comment(conn, tid, "kanban-dispatcher", f"Woken: the job finished: succeeded ({URL}). ")
        _replicate_with_uid(conn, tid, cid, "01a1f9cd-c774-709c-b90d-720d0241b4b4", len_delta=1)
        conn.execute("UPDATE task_comments SET uid = NULL WHERE task_id = ?", (tid,))
        assert prs.active_pr_reason(conn, tid, now + 700) == "active_pr"


def test_replica_own_run_pr_comment_mapped_by_uid_still_holds(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with kbc.connect() as conn:
        tid, run_id = _owner_parked(conn, monkeypatch, now)
        cid = _worker_comment(conn, tid, run_id, monkeypatch, "Opened https://github.com/example/project/pull/99 ")
        _replicate_with_uid(conn, tid, cid, "01a1f9cd-0000-7000-8000-000000000001", len_delta=1)
        assert prs.active_pr_reason(conn, tid, now + 700) == "active_pr"


def test_duplicate_uid_falls_back_and_fails_closed(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with kbc.connect() as conn:
        tid, _ = _owner_parked(conn, monkeypatch, now)
        cid = kb.add_comment(conn, tid, "kanban-dispatcher", f"Woken: the job finished: succeeded ({URL}). ")
        _replicate_with_uid(conn, tid, cid, "01a1f9cd-c774-709c-b90d-720d0241b4b4", len_delta=1)
        conn.execute("UPDATE task_comments SET uid = '01a1f9cd-c774-709c-b90d-720d0241b4b4' WHERE task_id = ?",
                     (tid,))
        rows = prs._comment_rows(conn, tid)
        # A hold alone can be supplied by duplicate-event protection even if the resolver
        # incorrectly picks the first UID hit. Assert the ambiguity fallback itself.
        event = {"comment_uid": "01a1f9cd-c774-709c-b90d-720d0241b4b4",
                 "comment_id": cid, "author": "kanban-dispatcher",
                 "len": len(f"Woken: the job finished: succeeded ({URL}). ")}
        assert prs._event_row_id(rows, now + 8, event) is None
        assert prs.active_pr_reason(conn, tid, now + 700) == "active_pr"


@pytest.mark.parametrize("body", ["  padded  ", "tail \n", "\nhead", "plain"])
def test_event_len_equals_stored_length(body):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        cid = kb.add_comment(conn, tid, "someone", body)
        stored = conn.execute("SELECT length(body) FROM task_comments WHERE id=?", (cid,)).fetchone()[0]
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='commented'", (tid,)).fetchone()[0])
        assert payload["len"] == stored


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_finished_note_has_no_surrounding_whitespace(outcome):
    rec = {"adapter": "github", "scope": "x"}
    derived = {"external": {"service": "GitHub Actions"}}
    kew_result = {"service": "GitHub Actions", "outcome": outcome, "url": URL}
    orig = kew._result
    kew._result = lambda rec, derived: kew_result
    try:
        note = kew._finished_note(rec, derived)
    finally:
        kew._result = orig
    assert note == note.strip()
    assert note.startswith("Woken: the job on GitHub Actions finished: " + outcome)
