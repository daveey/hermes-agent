"""Transfer exclusion also covers auxiliary producers, not just status UPDATEs."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from tests.hermes_cli import test_home_release_candidates as support

board = support.board


@pytest.mark.parametrize("phase", support.PHASES)
def test_shared_clear_retires_healthy_peer_only(board, monkeypatch, phase):
    from hermes_cli import kanban_shared_blockers as blockers
    task, neighbour = support.card(board, "ready"), support.card(board, "ready")
    support.freeze(board, task, phase)
    # Isolate the release consumer from external blocker observation. It still
    # performs its real per-row eligibility checks and native event writes.
    state = blockers.BlockerState(blocker_id="fixture", cleared_by=("other", 1), cleared_at=10,
                                 cleared_via="run", behind={task: 1, neighbour: 1})
    monkeypatch.setattr(blockers, "blocker_states", lambda *a, **k: {"fixture": state})
    assert blockers.release_cleared_blockers(board) == []
    assert not board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=?",
                             (task, blockers.CLEARED_EVENT)).fetchone()
    assert board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=?",
                         (neighbour, blockers.CLEARED_EVENT)).fetchone()


@pytest.mark.parametrize("phase", support.PHASES)
def test_notice_never_reads_placement_or_writes_frozen_target(board, monkeypatch, phase):
    from hermes_cli import kanban_parent_unclaimed as notices
    task = support.card(board, "ready")
    support.freeze(board, task, phase)
    monkeypatch.setattr(notices, "_place", lambda *a: pytest.fail("frozen target reached placement read"))
    before = board.total_changes
    assert not notices.post_notice(board, task, task, wait_key=1, waited_seconds=500, ready_seconds=500)
    assert board.total_changes == before


@pytest.mark.parametrize("phase", support.PHASES)
def test_stranded_producers_do_not_refile_or_escalate(board, phase):
    from hermes_cli import kanban_stranded_park as stranded
    task = support.card(board)
    support.freeze(board, task, phase)
    row = dict(board.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone())
    before = board.total_changes
    assert not stranded._escalate(board, row, None, now=9999999999, age=999999,
                                 defer=0, desk=None, contained=False)
    assert not stranded._refile(board, row, None, now=9999999999, age=999999)
    assert board.total_changes == before


@pytest.mark.parametrize("phase", support.PHASES)
def test_spend_promote_and_review_queue_refuse_before_accounting(board, monkeypatch, phase):
    from hermes_cli import kanban_spend_promote as promote, kanban_spend_reviewer as review
    task = support.card(board)
    support.freeze(board, task, phase)
    before = board.total_changes
    assert "home transfer" in promote.scope_refusal(board, task, "owner", "episode", 1)
    monkeypatch.setattr(review, "settings", lambda: (True, 5))
    with dbc.write_txn(board):
        assert not review.queue(board, task, "episode")
    assert board.total_changes == before


@pytest.mark.parametrize("phase", support.PHASES)
def test_stop_ended_preserves_transfer_custody(board, phase):
    from hermes_cli import kanban_stop_ended as stop
    task = support.card(board)
    with dbc.write_txn(board):
        board.execute("INSERT INTO task_runs(task_id,profile,status,outcome,started_at,ended_at,uid) "
                      "VALUES(?,'default','done','continued',1,2,'ended-fixture-uid')", (task,))
    support.freeze(board, task, phase)
    before = board.total_changes
    with pytest.raises(stop.StopRefused) as exc:
        stop._eligible(board, {"gateway_id": "worker-a3"}, stop.snapshot(board, task))
    assert "home transfer" in str(exc.value)
    assert board.total_changes == before


@pytest.mark.parametrize("phase", support.PHASES)
def test_recovery_requester_skips_before_creating_any_child(board, monkeypatch, phase):
    from hermes_cli import kanban_resume_requester as requester
    task = support.card(board)
    support.freeze(board, task, phase)
    # The real candidate query is independently covered by its own consumer
    # suite; inject a selected row to exercise the handoff exclusion boundary.
    monkeypatch.setattr(requester, "_candidates", lambda conn: [dict(conn.execute(
        "SELECT * FROM tasks WHERE id=?", (task,)).fetchone())])
    before = board.total_changes
    assert requester.ensure_recovery_cards(board) == []
    assert board.total_changes == before
