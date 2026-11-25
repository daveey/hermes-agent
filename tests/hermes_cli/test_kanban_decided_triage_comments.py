"""Recorded scope renewal must preserve the explicit promotion read boundary."""
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_spend_decision as decision
from hermes_cli.kanban_decided_triage import repair_decided_triage
from hermes_cli.kanban_release_comments import UnreadComments, check
from hermes_cli.kanban_triage_sweep import sweep_stale_triage
from tests.hermes_cli import test_kanban_scope_renewal as scope
from tests.hermes_cli import test_kanban_decided_triage as support

board = scope.board


def renewed(board, monkeypatch):
    tid, _, episode = scope.stopped(board, monkeypatch)
    scope.link(board, tid)
    scope.renew(board, tid, episode, scope.mandate(board, tid))
    return tid


def foreign_note(board, tid):
    return kb.add_comment(board, tid, 'default',
                          'Supervisor: HOLD, do not promote; superseded by another card.',
                          writer_kind='agent', writer_session='supervisor-session-x')


def test_agent_renewal_must_not_skip_pending_promotion_read(board, monkeypatch):
    tid = renewed(board, monkeypatch)
    cid = foreign_note(board, tid)
    with pytest.raises(UnreadComments):
        kb.promote_task(board, tid, actor='owner', reason='renewed',
                        precondition=lambda c: check(c, tid, session=scope.CHAT,
                                                     seen=None, verb='promote'))
    assert kb.get_task(board, tid).status == 'triage'
    sweep_stale_triage(board, older_than=999999)
    assert kb.get_task(board, tid).status == 'triage'
    assert decision.pending(board, tid)
    # The actual owner can read and explicitly acknowledge the exact comment;
    # maintenance never manufactures this acknowledgement on their behalf.
    promoted, refusal = kb.promote_task(
        board, tid, actor='owner', reason='read supervisor note and decided',
        precondition=lambda c: check(c, tid, session=scope.CHAT, seen=cid, verb='promote'))
    assert promoted, refusal
    assert kb.get_task(board, tid).status == 'ready'
    assert not decision.pending(board, tid)


def test_unread_card_does_not_starve_authorized_neighbor(board, monkeypatch):
    tid = renewed(board, monkeypatch)
    foreign_note(board, tid)
    healthy = support.stopped(board)
    support.authorize(board, healthy)
    assert repair_decided_triage(board) == [healthy]
    assert kb.get_task(board, tid).status == 'triage'
    assert decision.pending(board, tid)
    assert kb.get_task(board, healthy).status == 'ready'


def test_own_session_comment_does_not_block_repair(board, monkeypatch):
    tid = renewed(board, monkeypatch)
    kb.add_comment(board, tid, 'default', 'Own progress note.',
                   writer_kind='agent', writer_session=scope.CHAT)
    assert repair_decided_triage(board) == [tid]
    assert kb.get_task(board, tid).status == 'ready'


def test_comment_arriving_at_writer_recheck_retains_hold(board, monkeypatch):
    tid = renewed(board, monkeypatch)
    original = kb.write_txn

    @contextmanager
    def raced(c, **kw):
        monkeypatch.setattr(kb, 'write_txn', original)
        foreign_note(c, tid)
        with original(c, **kw):
            yield

    monkeypatch.setattr(kb, 'write_txn', raced)
    assert repair_decided_triage(board) == []
    assert kb.get_task(board, tid).status == 'triage'
    assert decision.pending(board, tid)
    assert not board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='promoted_manual'",
                             (tid,)).fetchone()


def test_missing_foreign_comment_data_fails_closed(board, monkeypatch):
    tid = renewed(board, monkeypatch)
    cid = foreign_note(board, tid)
    with kb.write_txn(board):
        board.execute('DELETE FROM task_comments WHERE id=?', (cid,))
    before = board.total_changes
    assert repair_decided_triage(board) == []
    assert board.total_changes == before
    assert kb.get_task(board, tid).status == 'triage'
    assert decision.pending(board, tid)
