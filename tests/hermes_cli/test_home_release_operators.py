"""Frozen custody excludes sibling quota, hold and triage producers before effects."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from tests.hermes_cli import test_home_release_candidates as support

board = support.board


@pytest.mark.parametrize("phase", support.PHASES)
def test_quota_cleanup_preserves_frozen_stamp_and_progresses_peer(board, phase):
    from hermes_cli.kanban_db_dispatch_quota import clear_quota_error
    task, peer = support.card(board), support.card(board)
    with dbc.write_txn(board):
        for tid in (task, peer):
            board.execute("UPDATE tasks SET last_failure_error='quota wall' WHERE id=?", (tid,))
    support.freeze(board, task, phase)
    before = board.total_changes
    clear_quota_error(board, task, "quota wall")
    assert board.total_changes == before
    clear_quota_error(board, peer, "quota wall")
    assert kb.get_task(board, task).last_failure_error == "quota wall"
    assert kb.get_task(board, peer).last_failure_error is None


@pytest.mark.parametrize("phase", support.PHASES)
def test_quota_and_hold_release_refuse_before_external_reads(board, monkeypatch, phase):
    from hermes_cli import kanban_quota_episode_release as quota, kanban_hold_release as hold
    from hermes_cli import kanban_operator_resume as operator, kanban_db_dispatch as dispatch
    task = support.card(board, "ready")
    support.freeze(board, task, phase)
    monkeypatch.setattr(operator, "worker_lineage", lambda: None)
    monkeypatch.setattr(dispatch, "check_respawn_guard", lambda *a: pytest.fail("frozen hold read GitHub"))
    before = board.total_changes
    with pytest.raises(quota.ReleaseRefused, match="home transfer"):
        quota.release(board, task, reason="operator", actor="human", probe=lambda *a: pytest.fail("live quota read"))
    with pytest.raises(quota.ReleaseRefused, match="home transfer"):
        quota._probe(board, task, lambda *a: pytest.fail("live quota read"), "human")
    with pytest.raises(ValueError, match="home transfer"):
        hold.release(board, task, supervisor="owner", supervisor_board="default", precondition=lambda c: None)
    with pytest.raises(operator.ResumeRefused, match="home transfer"):
        operator.resume(board, task, reason="operator", actor="human")
    assert board.total_changes == before


@pytest.mark.parametrize("phase", support.PHASES)
def test_review_preparation_skips_before_reading_park(board, monkeypatch, phase):
    from hermes_cli import kanban_review_release as review
    task = support.card(board, "todo")
    support.freeze(board, task, phase)
    monkeypatch.setattr(review, "_park", lambda *a: pytest.fail("frozen card reached review preparation"))
    before = board.total_changes
    assert review.prepare(board) == {}
    assert board.total_changes == before


@pytest.mark.parametrize("phase", support.PHASES)
def test_triage_cap_and_contained_prelude_create_no_children(board, monkeypatch, phase):
    from hermes_cli import kanban_triage_sweep as triage
    task = support.card(board)
    support.freeze(board, task, phase)
    row = dict(board.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone())
    monkeypatch.setattr(triage, "stranded_contained", lambda *a, **k: [dict(row, episode="old")])
    monkeypatch.setattr(triage, "_contained_chat_owners", lambda *a: pytest.fail("frozen source reached owner lookup"))
    before = board.total_changes
    assert triage._cap(board, row, 9999999999) == ""
    assert triage.escalate_stranded_contained(board) == []
    assert board.total_changes == before


@pytest.mark.parametrize("phase", support.PHASES)
def test_containment_resume_refuses_before_bootstrap(board, monkeypatch, phase):
    from hermes_cli import kanban_containment_lifecycle as lifecycle
    task = support.card(board)
    support.freeze(board, task, phase)
    monkeypatch.setattr(lifecycle, "install_local", lambda *a: pytest.fail("frozen target reached bootstrap"))
    before = board.total_changes
    with pytest.raises(lifecycle.ContainmentRefused, match="home transfer"):
        lifecycle.resume_local(board, target={"gateway": "local", "board": "default", "task_id": task},
                               episode="old", actor_task="owner", actor_run_uid="run", kind="authority")
    assert board.total_changes == before
