"""Hand-filed worker children inherit requester urgency without a dependency edge."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools import kanban_tools as tools
from tests.tools import test_kanban_worker_created_chats as helpers

home = helpers.home


@pytest.mark.parametrize('argument,intent,expected', [
    ("omitted", "omitted", 37), (0, "explicit", 0), (5, "explicit", 5),
    (None, "explicit", 37), (60000, "explicit", 1000)])
@pytest.mark.parametrize('creator_present', [True, False])
def test_capture_keeps_original_tool_intent(home, monkeypatch, argument, intent, expected, creator_present):
    from hermes_cli import kanban_fairness_journal as journal
    from hermes_cli import kanban_fairness_runtime as runtime

    with kbc.connect_closing() as conn:
        creator = (kb.create_task(conn, title='creator', priority=37, session_id=helpers.CHAT)
                   if creator_present else 't_absent_on_target_board')
        journal.arm(conn, 'tool-intent')
    config = {'kanban': {'fairness_capture': {'enabled': True, 'capture_id': 'tool-intent',
                                           'directory': str(home / 'capture')}}}
    session = runtime.activate(config, home=home, board='default', source_gateway='test', board_uid='board')
    monkeypatch.setenv('HERMES_KANBAN_TASK', creator)
    # A missing creator on this board still has a genuine creating chat. The
    # absent creator remains in the capture; its priority must stay unknown.
    monkeypatch.setenv('HERMES_SESSION_ID', helpers.WORKER if creator_present else helpers.CHAT)
    try:
        args = {'title': 'child', 'assignee': 'default', 'requires': [], 'placement': 'local'}
        if argument != 'omitted':
            args['priority'] = argument
        result = json.loads(tools._handle_create(args))
        assert result.get('ok'), result
        with kbc.connect_closing() as conn:
            records = [json.loads(row[5]) for row in journal.rows(conn) if row[1] == 'creation']
        assert not session.abandoned.is_set()
        assert len(records) == 1
        assert records[0]['creator_task_uid'] == creator
        if not creator_present:
            assert 'creator_task_uid' not in records[0]['unknown_fields']
            assert records[0]['effective_parent_priority'] is None
            assert 'effective_parent_priority' in records[0]['unknown_fields']
            if argument in ('omitted', None):
                expected = 0
        assert records[0]['priority_intent'] == intent
        assert records[0]['priority_argument'] == (None if argument == 'omitted' else argument)
        assert records[0]['stored_child_priority'] == expected
    finally:
        session.close()



@pytest.mark.parametrize('explicit,expected', [(None, 37), (0, 0), (11, 11), (60000, 1000)])
def test_tool_default_inherits_but_explicit_priority_wins(home, monkeypatch, explicit, expected):
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title='creator', priority=37, session_id=helpers.CHAT)
    monkeypatch.setenv('HERMES_KANBAN_TASK', creator)
    monkeypatch.setenv('HERMES_SESSION_ID', helpers.WORKER)
    args = {'title': 'child', 'assignee': 'default', 'requires': [], 'placement': 'local'}
    if explicit is not None:
        args['priority'] = explicit
    result = json.loads(tools._handle_create(args))
    assert result.get('ok'), result
    with kbc.connect_closing() as conn:
        child = kb.get_task(conn, result['task_id'])
        assert child.priority == expected
        assert conn.execute('SELECT COUNT(*) FROM task_links WHERE child_id=?', (child.id,)).fetchone()[0] == 0


def test_tool_inherits_donated_priority_not_age_or_class(home, monkeypatch):
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title='creator', priority=0, session_id=helpers.CHAT,
                                 admission_class='urgent')
        kb.create_task(conn, title='waiting', priority=83, parents=[creator])
    monkeypatch.setenv('HERMES_KANBAN_TASK', creator)
    monkeypatch.setenv('HERMES_SESSION_ID', helpers.WORKER)
    result = json.loads(tools._handle_create({'title': 'child', 'assignee': 'default', 'requires': [],
                                              'placement': 'local'}))
    assert result.get('ok'), result
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, result['task_id']).priority == 83


def test_inheritance_crosses_placement_and_courier_without_owner_reinference(home, monkeypatch):
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title='requester', priority=37, session_id=helpers.CHAT)
        unrelated = kb.create_task(conn, title='owner process task', priority=999)
    monkeypatch.setenv('HERMES_KANBAN_TASK', creator)
    monkeypatch.setenv('HERMES_SESSION_ID', helpers.WORKER)
    sent = []

    def transport(args, **kwargs):
        sent.append(dict(args))
        return json.dumps({'ok': True, 'transport_fixture': True}), None

    with monkeypatch.context() as route:
        route.setattr(tools, '_place_remote', transport)
        result = json.loads(tools._handle_create({'title': 'transported child', 'requires': [],
                                                  'assignee': 'default'}))
    assert result['transport_fixture']
    assert len(sent) == 1 and sent[0]['priority'] == 37
    # Trusted owner receives the already serialized priority; its ambient worker
    # must not donate its unrelated 999. Exercise creation, not just the helper.
    monkeypatch.setenv('HERMES_KANBAN_TASK', unrelated)
    intent = {'request_id': 'priority-transport-fixture',
              'owner': {'gateway': 'owner', 'profile': 'default', 'board': 'default'},
              'origin': {'gateway': 'sender', 'profile': 'default', 'session': helpers.CHAT}}
    from hermes_cli import kanban_fairness_journal as journal
    from hermes_cli import kanban_fairness_runtime as runtime

    with kbc.connect_closing() as conn:
        journal.arm(conn, 'courier-intent')
    config = {'kanban': {'fairness_capture': {'enabled': True, 'capture_id': 'courier-intent',
                                           'directory': str(home / 'courier-capture')}}}
    session = runtime.activate(config, home=home, board='default', source_gateway='test', board_uid='board')
    assert session is not None
    observed_receipts = []
    inherit = tools.inherit_tool_priority

    def observe_courier(*args, **kwargs):
        observed_receipts.append(kwargs.get('receipt'))
        return inherit(*args, **kwargs)

    monkeypatch.setattr(tools, 'inherit_tool_priority', observe_courier)
    try:
        result = json.loads(tools._handle_create(sent[0], origin_intent=intent))
        assert observed_receipts == [None]  # do not collect normalized courier intent, even before DB filtering
        # Persisted creation is distinct from the pending origin-binding acknowledgement.
        assert result.get('created') and result.get('code') == 'origin_bind_pending', result
        with kbc.connect_closing() as conn:
            child = kb.get_task(conn, result['task_id'])
            assert child is not None and child.priority == 37
            records = [json.loads(row[5]) for row in journal.rows(conn) if row[1] == 'creation']
        assert not session.abandoned.is_set()
        assert len(records) == 1
        assert records[0]['priority_intent'] == 'unknown'
        assert records[0]['priority_argument'] is None
        assert {'priority_intent', 'priority_argument', 'creator_task_uid'} <= set(records[0]['unknown_fields'])
        assert records[0]['stored_child_priority'] == 37
    finally:
        session.close()
