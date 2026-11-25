"""Read real budget evidence after a previously missing prior-home stop is restored."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_override_fence as fence
from hermes_cli.kanban_release_comments import UnreadComments
from tests.hermes_cli import test_kanban_spend_override_fence as helpers
from tests.hermes_cli import test_kanban_scope_renewal as scope

board = scope.board
kanban_home = helpers.kanban_home


def _legacy(conn):
    tid = kb.create_task(conn, title='Restored stop', assignee='default')
    helpers._write(conn, tid, 10)
    comment = kb.list_comments(conn, tid)[-1]
    with kb.write_txn(conn):
        # Persist the historical shape: no episode was visible to the writer.
        conn.execute("UPDATE task_events SET payload=json_remove(payload,'$.decision_comment') "
                     "WHERE task_id=? AND kind=?", (tid, budget.OVERRIDE_EVENT))
        last = conn.execute('SELECT * FROM task_events WHERE task_id=? AND kind=?',
                            (tid, budget.OVERRIDE_EVENT)).fetchone()
        kb._append_event(conn, tid, budget.EVENT, {'reason': 'restored prior-home stop'})
        conn.execute('UPDATE task_events SET created_at=? WHERE task_id=? AND kind=?',
                     (last['created_at'] - 100, tid, budget.EVENT))
    return tid, comment, last


@pytest.mark.parametrize('synced', [False, True], ids=['local', 'synced-remapped'])
def test_restored_stop_requires_actual_prior_budget_comment_read(kanban_home, synced):
    if synced:
        from tests.hermes_cli import kanban_sync_support as support
        support.write_sync_config(kanban_home, True)
    with kbc.connect_closing() as conn:
        if synced:
            from hermes_cli import kanban_db_sync as sync
            from hermes_cli import kanban_sync_client as client
            sync.install_or_remove(conn, str(kanban_home / 'kanban.db'))
            client.ensure_identity(conn, 'default', 'fixture')
        tid, comment, _ = _legacy(conn)
        if synced:
            with kb.write_txn(conn):
                conn.execute("UPDATE task_events SET payload=json_set(payload,'$.comment_id',999999) "
                             "WHERE task_id=? AND kind='commented'", (tid,))
        with pytest.raises(UnreadComments) as refused:
            fence.check(conn, tid)
        assert refused.value.comments[0]['id'] == comment.id
        assert refused.value.comments[0]['body'] == comment.body
        with pytest.raises(UnreadComments):
            fence.check(conn, tid, seen=comment.id - 1)
        assert fence.check(conn, tid, seen=comment.id)
        helpers._write(conn, tid, 8, seen=comment.id)
        assert budget.card_override(conn, tid)['runs'] == 8


def test_nonstopped_budget_always_carries_portable_comment_reference(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='No visible stop yet', assignee='default')
        receipt = helpers._write(conn, tid)
        comment = kb.list_comments(conn, tid)[-1]
        assert receipt['decision_comment']['comment_id'] == comment.id
        assert 'episode' not in receipt


@pytest.mark.parametrize('damage', ['text', 'author', 'time', 'duplicate', 'episode', 'missing', 'explicit',
                                   'by', 'reason', 'reviewer', 'before-stop', 'prefix'])
def test_legacy_prior_comment_recovery_is_narrow(kanban_home, damage):
    with kbc.connect_closing() as conn:
        tid, comment, last = _legacy(conn)
        with kb.write_txn(conn):
            _damage(conn, tid, comment, last, damage)
        with pytest.raises(UnreadComments, match='unavailable'):
            fence.check(conn, tid, seen=comment.id)


def _damage(conn, tid, comment, last, damage):
    statements = {
        'text': ("UPDATE task_comments SET body='Unrelated budget' WHERE id=?", (comment.id,)),
        'author': ("UPDATE task_events SET payload=json_set(payload,'$.author','other') "
                   "WHERE task_id=? AND kind='commented'", (tid,)),
        'time': ("UPDATE task_events SET created_at=created_at-1 WHERE task_id=? AND kind='commented'", (tid,)),
        'prefix': ("UPDATE task_comments SET body=replace(body,'runs 10','runs 11') WHERE id=?", (comment.id,)),
        'episode': ("UPDATE task_events SET payload=json_set(payload,'$.episode','modern-stop') WHERE id=?",
                    (last['id'],)),
        'missing': ('DELETE FROM task_comments WHERE id=?', (comment.id,)),
        'explicit': ("UPDATE task_events SET payload=json_set(payload,'$.decision_comment',json(?)) WHERE id=?",
                     (json.dumps({'comment_uid': 'missing-explicit-ref'}), last['id'])),
    }
    if damage in statements:
        conn.execute(*statements[damage])
    elif damage in ('by', 'reason', 'reviewer'):
        data = json.loads(last['payload'])
        data['by' if damage == 'reviewer' else damage] = ('kanban-spend-reviewer' if damage == 'reviewer' else '')
        body = (f"Spend budget for this card: {budget._override_text(data)}, "
                f"set by {data['by']}: {data['reason']}. legacy window.")
        conn.execute('UPDATE task_comments SET body=? WHERE id=?', (body, comment.id))
        conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(data), last['id']))
    else:
        if damage == 'duplicate':
            note = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='commented'",
                                (tid,)).fetchone()[0]
            kb._append_event(conn, tid, 'commented', json.loads(note))
        else:
            conn.execute('UPDATE task_events SET created_at=? WHERE task_id=? AND kind=?',
                         (last['created_at'], tid, budget.EVENT))
        # Local fixture: put the budget after the duplicate note/restored stop.
        conn.execute('UPDATE task_events SET id=id+1000 WHERE id=?', (last['id'],))


def test_real_release_consumes_recovered_budget_read(board, monkeypatch):
    from hermes_cli import kanban_release as release
    from hermes_cli import kanban_spend_decision as decision

    tid, comment, _ = _legacy(board)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
    monkeypatch.setattr(release, '_authority', lambda *a: ('owning_chat', []))
    writer = ('agent', 'fixture-owner', 'fixture')
    refused = release.release(board, tid, writer=writer)
    assert refused['refusal'] == 'budget_decision_unread'
    assert str(comment.id) in refused['message'] and 'unavailable' not in refused['message']
    out = release.release(board, tid, writer=writer, seen=comment.id)
    assert out['outcome'] == 'released', out
    assert kb.get_task(board, tid).status == 'ready'
    assert not decision.pending(board, tid)
