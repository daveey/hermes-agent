"""Exercise claim containment rechecks after preflight but before the CAS."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_sync import uuid7
from tests.hermes_cli import test_kanban_containment_maintenance as fixtures

board = fixtures.board


@pytest.mark.parametrize("late_hold", [False, True])
def test_triage_sweep_contains_before_spend_or_late_promotion(board, monkeypatch, late_hold):
    from hermes_cli import kanban_spend_decision as spend
    from hermes_cli import kanban_triage_sweep as sweep
    from hermes_cli import kanban_goal_recovery as recovery
    from hermes_cli import kanban_resume_requester as requester
    from hermes_cli import kanban_triage_cleanup as cleanup
    task = kb.create_task(board, title="Fixture protected sweep", assignee="default")
    board.execute("UPDATE tasks SET status='triage' WHERE id=?", (task,))
    monkeypatch.setattr(sweep, "_gate_exclusion", lambda: "")
    monkeypatch.setattr(recovery, "current_park", lambda *_: None)
    monkeypatch.setattr(requester, "open_recovery_card", lambda *_: None)
    monkeypatch.setattr(cleanup, "archive_requested_fixture", lambda *_: False)
    calls = []

    def pending(conn, tid):
        calls.append(tid)
        if late_hold and len(calls) == 2:
            conn.execute("UPDATE tasks SET containment_episode=? WHERE id=?", (uuid7(), tid))
        return not late_hold

    def forbidden(*_a, **_k):
        pytest.fail("contained task reached spend producer routing")

    monkeypatch.setattr(spend, "pending", pending)
    monkeypatch.setattr(spend, "safe_ensure_producer", forbidden)
    if not late_hold:
        board.execute("UPDATE tasks SET containment_episode=? WHERE id=?", (uuid7(), task))
    assert sweep.sweep_stale_triage(board, now=10**11) == []
    assert calls == ([task, task] if late_hold else [])
    row = kb.get_task(board, task)
    assert row is not None and row.status == "triage"
    assert not board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='triage_swept'", (task,)).fetchone()


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_claim_rechecks_marker_inside_admitted_transaction(board, monkeypatch, lane):
    task = kb.create_task(board, title="Fixture transaction boundary", assignee="default")
    board.execute("UPDATE tasks SET status=? WHERE id=?", (lane, task))
    original = kb.claim_transaction
    entered = []

    @contextmanager
    def changed(conn, **kwargs):
        with original(conn, **kwargs) as admitted:
            assert admitted
            entered.append(True)
            conn.execute("UPDATE tasks SET containment_episode=? WHERE id=?", (uuid7(), task))
            yield admitted

    def forbidden(*_a, **_k):
        pytest.fail("contained claim crossed its in-transaction guard to the CAS")

    monkeypatch.setattr(kb, "claim_transaction", changed)
    monkeypatch.setattr(kb, "claim_and_open_run", forbidden)
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    assert claim(board, task) is None
    assert entered == [True]
    row = board.execute("SELECT status,current_run_id FROM tasks WHERE id=?", (task,)).fetchone()
    assert tuple(row) == (lane, None)
    assert not board.execute("SELECT 1 FROM task_runs WHERE task_id=?", (task,)).fetchone()
