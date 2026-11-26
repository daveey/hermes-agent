"""Scheduled review waits reach status and sidebar clocks without inventing execution."""
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_session_activity_timers as timers
from hermes_cli.kanban_session_wait_rows import _local_wait_info
import hermes_state_outcome as outcome
from hermes_state_session_status import reduce_status


@pytest.fixture
def board():
    conn = sqlite3.connect(':memory:')
    conn.executescript('''
      CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, awaiting TEXT, created_on TEXT);
      CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at REAL);
    ''')
    conn.execute('INSERT INTO tasks VALUES (?, ?, ?, ?, ?)',
                 ('owner', 'Waiting on review', 'scheduled', json.dumps({'kind': 'card', 'ref': 'review', 'gateway': 'hub'}), 'hub'))
    conn.execute('INSERT INTO tasks VALUES (?, ?, ?, NULL, ?)', ('review', 'Independent review', 'running', 'hub'))
    conn.execute('INSERT INTO task_events VALUES (1, ?, ?, ?, ?)', ('owner', 'scheduled', '{}', 100))
    yield conn
    conn.close()


@pytest.mark.parametrize('producer_state', ['running', 'review'])
def test_real_park_wait_drives_timer_and_canonical_status(board, monkeypatch, producer_state):
    board.execute("UPDATE tasks SET status=? WHERE id='review'", (producer_state,))
    info = _local_wait_info(board, ['owner'], now=820)['owner']
    assert info['producer_wait'] == {'id': 'review', 'since': 100}
    raw = timers.local(board, {('ordinary', 'owner'), ('kanban_owner_worker', 'owner')})['owner']
    assert timers.card_timer(raw) == {'id': 'owner', 'title': 'Waiting on review', 'state': 'waiting', 'since': 100}
    from hermes_cli import kanban_session_activity as activity
    monkeypatch.setattr(activity, 'session_outcome_tasks', lambda *a, **k: [
        {'id': 'owner', 'title': 'Waiting on review', 'status': 'scheduled', 'wait': info}])
    waits = outcome._card_items(['ordinary'])[1]
    assert waits[0]['state'] == 'producer_wait'
    identity = {'gateway': 'hub', 'profile': 'default', 'session_id': 'ordinary', 'owner_epoch': '1'}
    status = reduce_status({'identity': identity, 'generation': 1, 'work': waits, 'sources': {}})
    assert status['kind'] == 'waiting'
    assert status['presentation']['stripe'] == 'external'
    fixture = json.loads((Path(__file__).parents[2] / 'schemas/fixtures/producer-wait-sidebar.json').read_text())
    assert fixture == {'kind': status['kind'], 'stripe': status['presentation']['stripe'],
                       'timer': timers.card_timer(raw)}


@pytest.mark.parametrize('state', ['done', 'archived', 'unknown', 'blocked', 'todo'])
def test_inactive_producer_never_claims_live_wait(board, state):
    board.execute('UPDATE tasks SET status=? WHERE id=?', (state, 'review'))
    assert 'producer_wait' not in _local_wait_info(board, ['owner'], now=820)['owner']
    assert timers.card_timer(timers.local(board, {('ordinary', 'owner')})['owner']) is None


def test_owner_transition_and_unknown_remove_wait_clock(board):
    board.execute("UPDATE tasks SET status='running' WHERE id='owner'")
    assert _local_wait_info(board, ['owner'], now=820) == {}
    assert timers.card_timer(timers.local(board, {('ordinary', 'owner')})['owner']) is None
    board.execute("UPDATE tasks SET status='done' WHERE id='owner'")
    assert timers.card_timer(timers.local(board, {('ordinary', 'owner')})['owner']) is None


def test_wrong_gateway_and_missing_episode_do_not_fabricate_time(board):
    board.execute("UPDATE tasks SET created_on='other' WHERE id='review'")
    assert 'producer_wait' not in _local_wait_info(board, ['owner'], now=820)['owner']
    board.execute("UPDATE tasks SET created_on='hub' WHERE id='review'")
    board.execute('DELETE FROM task_events')
    assert 'producer_wait' not in _local_wait_info(board, ['owner'], now=820)['owner']


def test_replica_capture_and_stale_unknown_controls(board):
    from hermes_cli.kanban_producer_wait import stamp
    from hermes_cli.kanban_session_activity_remote_wait import _remote_wait
    row = {'id': 'owner', 'title': 'Waiting on review', 'status': 'scheduled'}
    stamp(board, [row])
    assert timers.card_timer(row)['state'] == 'waiting'
    assert _remote_wait(row)['producer_wait']['since'] == 100
    assert timers.card_timer({**row, 'stale': True}) is None
    assert timers.card_timer({**row, 'partial_source_unknown': 'unavailable'}) is None
    assert _remote_wait({**row, 'stale': True}) is None
    board.execute('UPDATE task_events SET created_at=900')
    assert 'producer_wait' not in _local_wait_info(board, ['owner'], now=820)['owner']
