"""Scoped recovery of a replaced review; never a generic READY unblock."""
import time
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli import test_kanban_dependency_review as reviews
from tests.tools import test_kanban_owner_wake_requeue as wake

board = wake.board


def held(conn, monkeypatch):
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    task, old = reviews.parked(conn)
    # Original result is administratively completed, not a claimed review.
    for parent in old:
        assert kb.complete_task(conn, parent, summary="Old review", metadata={"reviewed_head": "a" * 40})
    replacement = kb.create_task(conn, title="replacement review", assignee="reviewer")
    kb.link_tasks(conn, replacement, task)
    reviews.finish_reviews(conn, [replacement], metadata={
        "reviewed_pr": reviews.PR, "reviewed_sha": "a" * 40, "verdict": "approve"})
    assert kb.get_task(conn, task).status == "ready"
    assert dispatch.check_respawn_guard(conn, task) == "active_pr"
    sup = wake._supervisor(conn, kb, [wake._line(f"default/{task}")])
    wake._as_worker(monkeypatch, conn, kb, sup)
    return task, replacement, sup


@pytest.mark.parametrize("defect", ["short_sha", "absent_pr", "unrelated_pr", "unclaimed", "before_park",
                                  "revoked", "unlisted", "foreign", "claim_race", "new_pr"])
def test_release_refuses_invalid_evidence(board, monkeypatch, defect):
    from hermes_cli import kanban_wake_check
    from tests.hermes_cli.kanban_guard_helpers import worker_comment
    with kbc.connect() as conn:
        task, replacement, sup = held(conn, monkeypatch)
        now = int(time.time()) + 2
        monkeypatch.setattr(time, "time", lambda: now)
        metadata = {
            "short_sha": {"reviewed_pr": reviews.PR, "reviewed_sha": "abc"},
            "absent_pr": {"reviewed_sha": "a" * 40},
            "unrelated_pr": {"reviewed_pr": reviews.PR + "9", "reviewed_sha": "a" * 40},
        }
        sql = {
            "unclaimed": ("DELETE FROM task_events WHERE task_id=? AND kind='claimed'", replacement),
            "before_park": ("UPDATE task_runs SET ended_at=1 WHERE task_id=?", replacement),
            "revoked": ("UPDATE tasks SET current_run_id=NULL WHERE id=?", sup),
            "unlisted": ("UPDATE tasks SET body='No grants' WHERE id=?", sup),
            "claim_race": ("UPDATE tasks SET claim_lock='other' WHERE id=?", task),
        }
        if defect in metadata:
            conn.execute("UPDATE task_runs SET metadata=? WHERE task_id=?", (json.dumps(metadata[defect]), replacement))
        if defect in sql:
            statement, target = sql[defect]
            conn.execute(statement, (target,))
        if defect == "foreign":
            monkeypatch.setattr(kanban_wake_check, "foreign_copy", lambda *_: True)
        if defect == "new_pr":
            worker_comment(conn, task, "dev", reviews.PR + "9")
        conn.commit()
        out = wake._unblock(task)
        assert out.get("ok") is not True, out
        assert not any(e.kind == "hold_released" for e in kb.list_events(conn, task))


def test_exact_target_release_is_single_use(board, monkeypatch):
    with kbc.connect() as conn:
        task, _, _ = held(conn, monkeypatch)
        # Explicitly later than publication, including on second-resolution stores.
        now = int(time.time()) + 2
        monkeypatch.setattr(time, "time", lambda: now)
        out = wake._unblock(task)
        assert out.get("ok") is True, out
        assert out["via"] == "owner-wake-hold-release"
        assert kb.get_task(conn, task).status == "ready"
        assert dispatch.check_respawn_guard(conn, task) is None
        assert wake._unblock(task).get("ok") is not True
        events = kb.list_events(conn, task)
        assert not any(e.kind == "unblocked" for e in events)
        assert sum(e.kind == "hold_released" for e in events) == 1
        assert kb.claim_task(conn, task) is not None
        assert dispatch.check_respawn_guard(conn, task) == "active_pr"
        assert kb.reclaim_task(conn, task, reason="Worker died", signal_fn=lambda *_: None)
        assert dispatch.check_respawn_guard(conn, task) == "active_pr"
