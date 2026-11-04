"""Admission-to-transaction changes cannot inherit a prior release grant."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from tests.tools import test_kanban_owner_wake_hold_release as cases
from tests.tools import test_kanban_owner_wake_requeue as wake

board = wake.board


@pytest.mark.parametrize("change,reason", [
    ("event", "admission changed"),
    ("revoke", "revoked"),
    ("unlist", "revoked"),
    ("claim", "unclaimed READY"),
])
def test_admission_race_rechecked_in_transaction(board, monkeypatch, change, reason):
    with kbc.connect() as conn:
        task, _, sup = cases.held(conn, monkeypatch)
        now = int(time.time()) + 2
        monkeypatch.setattr(time, "time", lambda: now)
        original = dispatch.check_respawn_guard

        def race(current, tid):
            result = original(current, tid)
            assert result == "active_pr"
            if change == "event":
                kb._append_event(current, tid, "race_probe", {})
            elif change == "revoke":
                current.execute("UPDATE tasks SET current_run_id=NULL WHERE id=?", (sup,))
            elif change == "unlist":
                current.execute("UPDATE tasks SET body='Revoked' WHERE id=?", (sup,))
            else:
                current.execute("UPDATE tasks SET claim_lock='new-owner' WHERE id=?", (tid,))
            current.commit()
            return result

        monkeypatch.setattr(dispatch, "check_respawn_guard", race)
        out = wake._unblock(task)
        assert reason in out.get("error", ""), out
        assert not any(e.kind == "hold_released" for e in kb.list_events(conn, task))


def test_release_does_not_suppress_recent_success(board, monkeypatch):
    with kbc.connect() as conn:
        task, _, _ = cases.held(conn, monkeypatch)
        now = int(time.time()) + 2
        monkeypatch.setattr(time, "time", lambda: now)
        assert wake._unblock(task).get("ok")
        # A success arriving after the park must still win over the release.
        conn.execute("UPDATE task_runs SET outcome='completed', ended_at=? WHERE task_id=?", (now, task))
        conn.commit()
        assert dispatch.check_respawn_guard(conn, task) == "recent_success"
