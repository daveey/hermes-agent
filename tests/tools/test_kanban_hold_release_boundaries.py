"""Exercise defensive predicates independently of the tool's earlier gates."""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_hold_release as release
from tests.tools import test_kanban_owner_wake_hold_release as cases
from tests.tools import test_kanban_owner_wake_requeue as wake

board = wake.board


@pytest.mark.parametrize("defect", ["missing", "outcome", "profile", "ended", "claimed"])
def test_park_boundary(board, monkeypatch, defect):
    with kbc.connect() as conn:
        task, _, _ = cases.held(conn, monkeypatch)
        if defect == "missing":
            conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='dependency_wait'", (task,))
        elif defect == "claimed":
            conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='claimed'", (task,))
        else:
            field, value = {"outcome": ("outcome", "completed"), "profile": ("profile", "other"),
                            "ended": ("ended_at", None)}[defect]
            conn.execute(f"UPDATE task_runs SET {field}=? WHERE task_id=?", (value, task))
        conn.commit()
        with pytest.raises(ValueError):
            release._park(conn, task)


@pytest.mark.parametrize("field,value", [("parents", "not-list"), ("parents", [None]),
                                        ("parents", ["absent"]), ("prs", "not-list"),
                                        ("prs", [None]), ("prs", ["https://example.com/pr"])])
def test_scope_boundary(board, monkeypatch, field, value):
    with kbc.connect() as conn:
        task, _, _ = cases.held(conn, monkeypatch)
        event = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='dependency_wait'",
                             (task,)).fetchone()
        payload = json.loads(event["payload"])
        payload["review_checkpoint"][field] = value
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), event["id"]))
        conn.commit()
        with pytest.raises(ValueError):
            release._scope(conn, task)


def test_unsatisfied_dependency(board, monkeypatch):
    with kbc.connect() as conn:
        task, parent, _ = cases.held(conn, monkeypatch)
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        conn.commit()
        with pytest.raises(ValueError, match="retained satisfied"):
            release._scope(conn, task)


@pytest.mark.parametrize("stamp", [None, "invalid", "future", "tie"])
def test_receipt_time_boundary(board, monkeypatch, stamp):
    with kbc.connect() as conn:
        task, _, _ = cases.held(conn, monkeypatch)
        now = int(time.time()) + 2
        monkeypatch.setattr(time, "time", lambda: now)
        assert wake._unblock(task).get("ok")
        value = now + 1 if stamp == "future" else now - 1 if stamp == "tie" else stamp
        if value is None:
            # SQLite requires a non-null event stamp; conversion still returns None.
            value = ""
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='hold_released'", (value, task))
        conn.commit()
        assert not release.released_after(conn, task, now - 1, [cases.reviews.PR])


@pytest.mark.parametrize("field,value", [("current_run_id", 123), ("claim_expires", 9999999999),
                                        ("status", "running")])
def test_release_target_boundary(board, monkeypatch, field, value):
    from hermes_cli import kanban_db_dispatch as dispatch
    with kbc.connect() as conn:
        task, _, sup = cases.held(conn, monkeypatch)
        conn.execute(f"UPDATE tasks SET {field}=? WHERE id=?", (value, task))
        conn.commit()
        monkeypatch.setattr(dispatch, "check_respawn_guard", lambda *_: "active_pr")
        with pytest.raises(ValueError, match="unclaimed READY"):
            release.release(conn, task, supervisor=sup, supervisor_board="default", precondition=lambda _: None)


def test_empty_url_scope_does_not_match(board, monkeypatch):
    with kbc.connect() as conn:
        task, _, _ = cases.held(conn, monkeypatch)
        now = int(time.time()) + 2
        monkeypatch.setattr(time, "time", lambda: now)
        assert wake._unblock(task).get("ok")
        assert not release.released_after(conn, task, now - 1, [])
        conn.execute("UPDATE task_events SET payload=json_set(payload, '$.prs', 'not-a-list') "
                     "WHERE task_id=? AND kind='hold_released'", (task,))
        conn.commit()
        assert not release.released_after(conn, task, now - 1, [cases.reviews.PR])


def test_no_run_receipt(board):
    with kbc.connect() as conn:
        task = kb.create_task(conn, title="No history", assignee="dev")
        assert not release.released_after(conn, task, 0, [cases.reviews.PR])
