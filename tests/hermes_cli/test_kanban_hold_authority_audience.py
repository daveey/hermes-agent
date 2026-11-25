"""A notice recipient must not be mistaken for a scope-renewal authority."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_decision_ask as helpers

kanban_home = helpers.kanban_home


def test_hold_audience_uses_real_renewal_reader(kanban_home, monkeypatch):
    from hermes_cli import kanban_origin
    from hermes_cli.kanban_chat_links import link_uid
    from hermes_cli.kanban_hold_audience import describe
    from hermes_cli.kanban_scope_renewal_cli import resolve_actor
    monkeypatch.setattr(kanban_origin, 'gateway_names', lambda: ('home',))
    with kbc.connect_closing() as conn:
        from tests.hermes_cli.test_kanban_create_scope_authority import _links_schema
        _links_schema(conn)
        tid = kb.create_task(conn, title='held', assignee='default')
        with kb.write_txn(conn):
            for sid, kind, released in [('owner', 'created', 0), ('observer', 'shepherd', 0),
                                        ('released', 'created', 1)]:
                conn.execute('INSERT INTO task_chat_links '
                             '(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) '
                             'VALUES (?,?,?,?,?,?,?,1)',
                             (link_uid(tid, 'away', 'default', sid, kind), tid, 'away', 'default', sid, kind, released))
        audience = describe(conn, tid)
        assert audience['chats'] == [{'gateway': 'away', 'profile': 'default', 'session': 'owner'}]
        for chat in audience['chats']:
            actor = resolve_actor(conn, tid, writer=('agent', chat['session'], '1:HERMES_SESSION_ID'),
                                  environs=[{'HERMES_SESSION_ID': chat['session']}],
                                  gateway=chat['gateway'], profile=chat['profile'])
            assert actor.authority == 'chat'






@pytest.mark.parametrize("profile,kind,released,expected", [
    (None, "created", 0, True),
    ("default", "created", 0, True),
    ("research", "created", 0, True),
    ("", "created", 0, False),
    (None, "created", 1, False),
    (None, "shepherd", 0, False),
])
def test_hold_audience_accepts_only_renewable_foreign_hash_links(
        kanban_home, monkeypatch, profile, kind, released, expected):
    from hermes_cli import kanban_origin
    from hermes_cli.kanban_chat_links import link_uid
    from hermes_cli.kanban_hold_audience import describe
    from hermes_cli.kanban_scope_renewal_cli import resolve_actor
    from tests.hermes_cli.test_kanban_create_scope_authority import _links_schema

    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("worker-a1", {"worker-a1", "hub"}))
    session = "local-" + "dc" * 32  # No worker-local interactive session registration.
    with kbc.connect_closing() as conn:
        _links_schema(conn)
        tid = kb.create_task(conn, title="foreign held owner", assignee="default", settle_owner_chat=False)
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_chat_links "
                         "(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                         "VALUES (?,?,?,?,?,?,?,1)",
                         (link_uid(tid, "hub", profile, session, kind), tid, "hub", profile, session, kind, released))
        audience = describe(conn, tid)
        assert audience["chats"] == ([{"gateway": "hub", "profile": profile or "default", "session": session}]
                                     if expected else [])
        if expected:
            actor = resolve_actor(conn, tid, writer=("agent", session, "1:HERMES_SESSION_ID"),
                                  environs=[{"HERMES_SESSION_ID": session}],
                                  gateway="hub", profile=profile or "default")
            assert actor.authority == "chat"


def test_local_hold_payload_has_authority_and_no_synthetic_guard(kanban_home):
    from hermes_cli import kanban_hold_notices
    from hermes_cli.kanban_spend_decision import notice
    with kbc.connect_closing() as conn:
        from tests.hermes_cli.test_kanban_create_scope_authority import _links_schema
        _links_schema(conn)
        tid = kb.create_task(conn, title='orphan', assignee='default')
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'spend_budget_triaged', {'cause': 'scope_stopped'})
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        before = conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
        kanban_hold_notices.sweep(conn)
        payload = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? "
                                          "AND kind='spend_budget_decision_ready'", (tid,)).fetchone()[0])
        assert payload['authority']['chats'] == []
        assert 'No recorded renewable agent authority' in notice(payload)
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before
