"""Renewal accepts portable comment UIDs without weakening frozen identity or authority."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_scope_renewal as renewal
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_scope_renewal as support

board = support.board


def portable_mandate(board, tid, **kw):
    from hermes_cli import kanban_db_sync as sync
    sync.apply_sync_schema_steps(board)
    old_id = support.mandate(board, tid, **kw)
    uid = sync.uuid7()
    board.execute('UPDATE task_comments SET uid=? WHERE id=?', (uid, old_id))
    board.execute("UPDATE task_events SET payload=json_set(payload,'$.comment_uid',?) "
                  "WHERE task_id=? AND kind='commented' AND json_extract(payload,'$.comment_id')=?",
                  (uid, tid, old_id))
    # A replica assigns its own row id; the commented event still names the original id and UID.
    local_id = old_id + 10000
    board.execute('UPDATE task_comments SET id=? WHERE id=?', (local_id, old_id))
    board.commit()
    return uid, local_id


@pytest.mark.parametrize('entry', ['api', 'cli'])
@pytest.mark.parametrize('reference', ['uid', 'int', 'numeric_string'])
def test_portable_mandate_and_local_id_renew_the_same_frozen_identity(
        board, monkeypatch, capsys, entry, reference):
    tid, _, episode = support.stopped(board, monkeypatch)
    support.link(board, tid)
    uid, local_id = portable_mandate(board, tid)
    ref = {'uid': uid, 'int': local_id, 'numeric_string': str(local_id)}[reference]
    if entry == 'cli':
        support.ancestry(monkeypatch, {'HERMES_SESSION_ID': support.CHAT}, {})
        monkeypatch.setenv('HERMES_SESSION_ID', support.CHAT)
        assert support.argv(tid, '--episode', episode, '--mandate-comment', str(ref),
                            '--reason', 'bounded correction', '--json') == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    else:
        payload = support.renew(board, tid, episode, ref, reason='bounded correction')
    assert payload['mandate_comment_uid'] == uid
    assert payload['mandate_comment_id'] == local_id
    assert payload['actor_session'] == support.CHAT
    assert payload['covered_restriction_uids'] == [episode]
    assert kb.get_task(board, tid).status == 'triage'
    assert decision.pending(board, tid) and not decision.execution_restricted(board, tid)
    # Frozen renewal readers must NOT treat a numeric local ID as a portable UID on a synced row.
    assert renewal._comment(board, tid, str(local_id)) is None
    board.execute("UPDATE task_comments SET body='expanded scope' WHERE uid=?", (uid,))
    board.commit()
    assert decision.execution_restricted(board, tid)


@pytest.mark.parametrize('case,code', [
    ('missing', 'mandate_missing'), ('other_task', 'mandate_missing'),
    ('wrong_session', 'mandate_authority'), ('wrong_gateway', 'mandate_authority'),
    ('stale', 'mandate_stale'), ('episode', 'episode_mismatch'),
    ('safety', 'safety_restricted'), ('unattributable', 'mandate_unattributable'),
])
def test_uid_lookup_keeps_mandate_and_stop_guards(board, monkeypatch, case, code):
    tid, _, episode = support.stopped(board, monkeypatch)
    support.link(board, tid)
    target = kb.create_task(board, title='other', assignee='default') if case == 'other_task' else tid
    uid, _ = portable_mandate(board, target, session='other' if case == 'wrong_session' else support.CHAT,
                               kind='human' if case == 'wrong_gateway' else 'agent')
    kw = {}
    uid = 'missing-comment-uid' if case == 'missing' else uid
    if case == 'wrong_gateway':
        from hermes_cli import kanban_origin
        monkeypatch.setattr(kanban_origin, 'gateway_names', lambda: ('this-gateway', set()))
        board.execute("UPDATE task_events SET origin_gateway='another-gateway' WHERE task_id=? AND kind='commented'", (tid,))
        kw['writer'] = support.HUMAN
    elif case in ('stale', 'safety'):
        kb._append_event(board, tid, 'decision_hold_restricted',
                         {'cause': 'safety_refusal' if case == 'safety' else 'scope_stopped', 'episode': episode})
    if case == 'episode':
        episode = 'wrong-episode'
    if case == 'unattributable':
        board.execute("DELETE FROM task_events WHERE task_id=? AND kind='commented'", (tid,))
    board.commit()
    support.refused(board, tid, episode, uid, code, **kw)
