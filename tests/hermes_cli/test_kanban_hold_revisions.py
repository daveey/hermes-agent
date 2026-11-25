"""Guidance revisions cannot be hidden by a delivered base hold."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_decision_ask as helpers

kanban_home = helpers.kanban_home


def _record_owner(conn, tid, chat):
    from tests.hermes_cli.test_kanban_create_scope_authority import _links_schema
    from hermes_cli.kanban_chat_links import link_uid
    from hermes_cli.kanban_create_authority import _gateway
    _links_schema(conn)
    gateway = _gateway(conn)
    with kb.write_txn(conn):
        conn.execute('INSERT INTO task_chat_links '
                     '(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) '
                     'VALUES (?,?,?,?,?,?,0,1)',
                     (link_uid(tid, gateway, 'default', chat, 'created'), tid, gateway, 'default', chat, 'created'))





def test_legacy_notice_does_not_prevent_real_owner_binding(kanban_home):
    from hermes_cli import kanban_hold_notices as notices
    from hermes_cli import kanban_spend_decision as decision
    with kbc.connect_closing() as conn:
        chat = helpers._session()
        tid = kb.create_task(conn, title='held', assignee='default', session_id=chat,
                             require_scope_authority=True)
        _record_owner(conn, tid, chat)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, decision.EVENT, {'cause': 'scope_stopped', 'reason': 'stop'})
            stop = decision.pending_episode(conn, tid)
            episode = stop.get('uid') or str(stop['id'])
            kb._append_event(conn, tid, decision.NOTICE, {'cause': 'scope_stopped', 'episode': episode})
            conn.execute('DELETE FROM kanban_notify_subs WHERE task_id=?', (tid,))
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        notices.sweep(conn)
        events = [e for e in kb.list_events(conn, tid) if e.kind == decision.NOTICE]
        assert len(events) == 2
        sub = conn.execute('SELECT * FROM kanban_notify_subs WHERE task_id=? AND chat_id=?', (tid, chat)).fetchone()
        assert sub is not None and sub['last_event_id'] < events[-1].id
        assert events[-1].payload['authority']['chats'][0]['session'] == chat
        from hermes_cli.kanban_create_authority import _accepts, _gateway
        assert _accepts(conn, tid, chat, _gateway(conn), 'default')
        notices.sweep(conn)
        assert len([e for e in kb.list_events(conn, tid) if e.kind == decision.NOTICE]) == 2
        # Even a complete authority payload must not hide a missing subscription.
        with kb.write_txn(conn):
            conn.execute('DELETE FROM kanban_notify_subs WHERE task_id=?', (tid,))
        notices.sweep(conn)
        sub = conn.execute('SELECT * FROM kanban_notify_subs WHERE task_id=? AND chat_id=?', (tid, chat)).fetchone()
        assert sub is not None and sub['last_event_id'] < events[-1].id
        assert len([e for e in kb.list_events(conn, tid) if e.kind == decision.NOTICE]) == 2


def test_missing_authority_never_advertises_scope_renewal_command():
    from hermes_cli.kanban_spend_decision import notice
    text = notice({'cause': 'scope_stopped', 'task_id': 'legacy'})
    assert 'No recorded renewable agent authority' in text
    assert 'hermes kanban renew-scope' not in text


@pytest.mark.parametrize('cause', ['scope_stopped', 'safety_refusal'])
def test_restriction_revises_local_and_creator_guidance(kanban_home, cause):
    from hermes_cli import kanban_hold_notices as notices
    from hermes_cli import kanban_spend_decision as decision
    with kbc.connect_closing() as conn:
        chat = helpers._session()
        owner = kb.create_task(conn, title='owner', assignee='default', session_id=chat,
                               require_scope_authority=True)
        tid = kb.create_task(conn, title='held', assignee='default', creator_task_id=owner)
        _record_owner(conn, tid, chat)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, decision.EVENT, {'cause': 'runs', 'reason': 'cap'})
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        notices.sweep(conn)
        stop = decision.pending_episode(conn, tid)
        episode = stop.get('uid') or str(stop['id'])
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'decision_hold_restricted',
                             {'cause': cause, 'episode': episode, 'reason': 'new restriction'})
        notices.sweep(conn)
        events = [e for e in kb.list_events(conn, tid) if e.kind == decision.NOTICE]
        assert len(events) == 2 and events[-1].payload['cause'] == cause
        comments = conn.execute('SELECT body FROM task_comments WHERE task_id=? ORDER BY id', (owner,)).fetchall()
        assert len(comments) == 2
        assert ('scope' if cause == 'scope_stopped' else 'no supported execution release') in comments[-1][0]
        assert comments[-1][0].startswith(f'{tid}: stopped itself: {cause}; awaiting a decision. ')
        assert 'then manually promote it' not in comments[-1][0]
        notices.sweep(conn)
        decision.ensure_producer(conn, tid)
        assert len([e for e in kb.list_events(conn, tid) if e.kind == decision.NOTICE]) == 2
        assert kb.get_task(conn, tid).status == 'triage'
