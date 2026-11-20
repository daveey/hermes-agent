"""Compose owner adoption, real park, publication and remote chat delivery."""
import json
import time

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_origin_creation import adopt_with_origin
from hermes_cli.kanban_origin_ledger import observe_owner_task
from tests.hermes_cli import test_kanban_decision_ask as helpers
from tests.tui_gateway import test_kanban_remote_wake as remote
from tui_gateway import kanban_remote_notices as notices

kanban_home = helpers.kanban_home
home = remote.home


def _publish_owner(conn, tid):
    task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    task.update(gateway="hub", profile="default", board="default", origin_bindings=[
        json.loads(r[0]) for r in conn.execute("SELECT binding_json FROM origin_creation_intents WHERE task_id=?", (tid,))])
    run = conn.execute("SELECT ended_at,summary FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    task['run'] = dict(run) if run else {}
    conn.execute('BEGIN')
    try:
        return observe_owner_task(conn, task, now=time.time())
    finally:
        conn.rollback()


@pytest.mark.parametrize('entry', ['creator', 'external_comment', 'inferred_comment', 'block_time', 'human'])
def test_adopted_owner_ask_reaches_origin_chat(kanban_home, home, monkeypatch, entry, caplog):
    import logging
    caplog.set_level(logging.DEBUG, logger='tui_gateway.kanban_remote_notices')
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        adopt_with_origin(conn, {
            'request_id': 'adopt:decision-proof-001',
            'owner': {'gateway': 'hub', 'profile': 'default', 'board': 'default', 'task_id': tid},
            'origin': {'gateway': 'macbook', 'profile': 'default', 'session': remote.CHAT},
        }, board='default')
        # A real durable binding and its owner-written subscription, not a fabricated link.
        assert conn.execute('SELECT 1 FROM origin_creation_intents WHERE task_id=?', (tid,)).fetchone()
        helpers._as_worker(monkeypatch, conn, tid)
        if entry != 'human':
            args = {
                'reason': 'Decide premise A/B/C; waiting on the next reply.',
                'wake_check': f'hermes kanban wait-comment {tid} --after-id 0 --skip-system',
            }
            if entry != 'inferred_comment':
                args['awaiting'] = {'kind': 'external' if entry == 'external_comment' else 'chat',
                                    'why': 'Decide premise A/B/C.', 'ref': 'origin session'}
            if entry == 'block_time':
                args['kind'] = 'time'
            out = helpers._tool('block' if entry == 'block_time' else 'schedule', args)
        else:
            out = helpers._tool('block', {
                'kind': 'needs_input', 'reason': 'Choose premise A/B/C.',
                'human_request': {
                    'action': 'Reply A or B. Unblock = use the pick; archive = drop it.',
                    'why_human': 'Only the user can choose the personal preference.',
                    'interaction': 'reply', 'scope': 'premise A/B/C', 'evidence': 'No preference recorded.',
                },
            })
        assert out.get('ok') is not False and 'error' not in out, out
        card = _publish_owner(conn, tid)
    remote._publish(home, [card])
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, 'gateway_names', lambda: ('macbook',))
    assert notices._linked_chats(card, 'macbook') == [('default', remote.CHAT)]
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import get_hermes_home
    assert get_profile_dir('default') == get_hermes_home(), (get_profile_dir('default'), get_hermes_home())
    assert notices._sweep_snapshot(home / 'hub.db', 'macbook')[0], card
    from tui_gateway import kanban_unloaded as ku
    wakes = []
    def wake(tip, text, metadata):
        wakes.append((tip, text, metadata))
        remote._turn_writes_row(tip, text, metadata)
        return 'started'
    monkeypatch.setattr(ku, 'wake_turn', wake)
    monkeypatch.setattr(ku, '_spawn_thread', lambda job: job())
    def sweep():
        return ku.sweep_remote_once(set(), grace_s=0, gateway='macbook')
    if entry == 'creator':
        # A decision must not be acked as a passive row when the agent cannot run.
        monkeypatch.setattr(ku, '_has_wake_budget', lambda *_: False)
        sweep()
        assert remote._notice_rows() == [] and wakes == []
        monkeypatch.setattr(ku, '_has_wake_budget', lambda *_: True)
    sweep()
    rows = remote._notice_rows()
    assert len(rows) == 1, (wakes, rows)
    text = rows[0]['content']
    assert 'premise A/B/C' in text
    if entry != 'human':
        assert [w[0] for w in wakes] == [remote.CHAT]
        assert 'creator agent' in text.lower() and 'kanban_comment' in text
        assert 'Next action for the user' not in text
    else:
        # Existing human-request policy deliberately presents the human ask without an agent turn.
        assert 'Next action for the user' in text
    assert sweep() == []
    notices._reset_for_tests()
    assert sweep() == []


def test_owner_envelope_does_not_resurrect_old_question(kanban_home):
    from hermes_cli.kanban_remote_ask import publish
    from hermes_cli.kanban_db_events import append_event
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        task = {'id': tid, 'status': 'scheduled'}
        append_event(conn, tid, 'decision_requested', {'why': 'first question'})
        first = publish(conn, task)
        assert first is not None and first['payload']['why'] == 'first question'
        append_event(conn, tid, 'scheduled', {})  # a later non-chat park supersedes it
        assert publish(conn, task) is None
        append_event(conn, tid, 'decision_requested', {'why': 'second question'})
        second = publish(conn, task)
        assert second is not None and second['event_id'] != first['event_id']
        assert publish(conn, {**task, 'status': 'running'}) is None
        from tui_gateway.kanban_remote_notice_state import state
        assert state({**task, 'ask_notice': first}) != state({**task, 'ask_notice': second})


@pytest.mark.parametrize('change', [
    {'kind': 'blocked'}, {'event_id': True}, {'event_id': -1},
    {'created_at': 'yesterday'}, {'created_at': float('nan')}, {'payload': 'not a mapping'},
])
def test_malformed_decision_envelope_is_not_notifiable(change):
    from hermes_cli.kanban_remote_ask import notifiable
    ask = {'kind': 'decision_requested', 'event_id': 1, 'created_at': 1.0, 'payload': {}}
    assert notifiable({'status': 'scheduled', 'ask_notice': ask})
    assert not notifiable({'status': 'scheduled', 'ask_notice': {**ask, **change}})
    assert not notifiable({'status': 'running', 'ask_notice': ask})

