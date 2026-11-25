"""Elapsed display evidence stays in the same lineage/count snapshot."""
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_activity_identity as identity
from hermes_cli import kanban_session_activity_timers as timers
from hermes_cli.kanban_activity_projection import project_kanban_activity
from hermes_cli.kanban_chat_links import _read_replica_links, replica_cards

ASK = {'action': 'Reply A or B', 'why_human': 'Only you know', 'interaction': 'reply',
       'scope': 'Choose a colour', 'evidence': 'Two choices'}


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    conn.executescript('''
        CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, started_at REAL,
          worker_started_at REAL, ready_since REAL, updated_at REAL, current_run_id INTEGER,
          block_kind TEXT, human_request TEXT, created_on TEXT, assignee TEXT, created_at REAL, priority INTEGER);
        CREATE TABLE task_runs (id INTEGER, task_id TEXT, started_at REAL);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, created_at REAL);
        CREATE TABLE task_chat_links (task_id TEXT, chat_session TEXT, chat_profile TEXT,
          chat_gateway TEXT, kind TEXT, released INTEGER);
    ''')
    yield conn
    conn.close()


def add(conn, tid, status='running', **fields):
    values = dict(id=tid, title='Title ' + tid, status=status, started_at=100, worker_started_at=80,
                  ready_since=200_000, updated_at=300, current_run_id=1,
                  block_kind='needs_input', human_request=json.dumps(ASK), created_on='worker') | fields
    conn.execute(f"INSERT INTO tasks ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
    conn.execute('INSERT INTO task_chat_links VALUES (?, ?, ?, ?, ?, 0)', (tid, 's', 'default', 'mac', 'created'))


def project(keys, row=None):
    row = row or {'id': 's', '_lineage_ids': ['old']}
    parts = ({}, {}, {}, {}, {}, {}, {}, {}, keys, {}, {}, {})
    project_kanban_activity([row], parts=parts, observed_at=400, now=400)
    return row


def test_local_run_current_attempt_and_block_event_precedence(db):
    add(db, 'a')
    add(db, 'b', 'blocked')
    db.executemany('INSERT INTO task_runs VALUES (?, ?, ?)', [(1, 'a', 150), (2, 'a', 350)])
    db.executemany('INSERT INTO task_events VALUES (?, ?, ?, ?)', [(1, 'b', 'blocked', 170),
        (2, 'b', 'blocked', 190), (3, 'b', 'heartbeat', 310)])
    pairs = {('s', 'a'), ('s', 'b'), ('old', 'a')}
    reads = {'pairs': pairs, 'timers': timers.local(db, pairs)}
    row = project(identity.local_keys(Path('board'), reads, board='default'))
    assert row['kanban_timed_cards'] == [
        {'id': 'default/a', 'title': 'Title a', 'state': 'running', 'since': 150},
        {'id': 'default/b', 'title': 'Title b', 'state': 'blocked', 'since': 190}]
    assert row['kanban_timed_more'] == 0


def test_replica_real_selection_and_projection(db, monkeypatch):
    add(db, 'a')
    add(db, 'b', 'blocked')
    db.execute('INSERT INTO task_runs VALUES (1, "a", 160)')
    db.execute('INSERT INTO task_events VALUES (1, "b", "blocked", 210)')
    db.row_factory = sqlite3.Row
    rows = _read_replica_links(db, 'mac', 'default')
    from hermes_cli import kanban_chat_links as links
    monkeypatch.setattr(links, '_read_replica', lambda *a: (rows, 400, {}))
    monkeypatch.setattr(links, '_released_links', lambda *a: [])
    cards = replica_cards(['s'], 'mac', replicas=[('default', Path('replica'))], profile='default',
                          origin_cards=[], now=400)
    from hermes_cli.fact_annotation_kanban import _remote
    maps, keys = tuple({} for _ in range(8)), {}
    _remote(cards, maps, keys, {})
    result = project(keys)['kanban_timed_cards']
    assert [(c['state'], c['since']) for c in result] == [('running', 160), ('blocked', 210)]
    assert all(c['id'].startswith('worker/default/default/') for c in result)


@pytest.mark.parametrize('status', ['blocked', 'triage'])
def test_fallback_and_only_live_human_requests(db, status):
    add(db, 'a', status)
    value = timers.local(db, {('s', 'a')})['a']
    assert timers.card_timer(value)['since'] == 200
    value['ready_since'] = None
    assert timers.card_timer(value)['since'] == 300
    value['human_request'] = None
    assert timers.card_timer(value) is None
    value.update(human_request=ASK, stale=True)
    assert timers.card_timer(value) is None


def test_run_fallbacks_and_invalid_timestamps(db):
    add(db, 'a')
    value = timers.local(db, {('s', 'a')})['a']
    assert timers.card_timer(value)['since'] == 100
    value['started_at'] = None
    assert timers.card_timer(value)['since'] == 80
    value['timer_worker_start'] = float('nan')
    assert timers.card_timer(value) is None


def test_cap_order_lineage_dedup_and_empty_clearing():
    markers = set()
    for i in range(9):
        markers.update(timers.marker({'id': str(i), 'status': 'running', 'started_at': 100 - i}))
    result = project({'s': markers, 'old': markers})
    assert len(result['kanban_timed_cards']) == 6
    assert result['kanban_timed_more'] == 3
    assert [c['since'] for c in result['kanban_timed_cards']] == [92, 93, 94, 95, 96, 97]
    project({}, result)
    assert 'kanban_timed_cards' not in result and 'kanban_timed_more' not in result


def test_count_transaction_carries_timers_into_the_real_row_annotator(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_session_activity as activity
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Timer integration', assignee='worker')
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running', session_id='s', started_at=150 WHERE id=?", (tid,))
        path = Path(str(kbc._main_db_file(conn)))
    activity._reset_cache_for_tests()
    monkeypatch.setattr(activity, '_board_db_paths', lambda: [path])
    monkeypatch.setattr(activity, '_remote_cards', lambda *a: [])
    result = activity.annotate_kanban_activity.__wrapped__([{'id': 's'}])
    assert result[0]['kanban_timed_cards'] == [
        {'id': 'default/' + tid, 'title': 'Timer integration', 'state': 'running', 'since': 150}]
    activity._reset_cache_for_tests()


def test_read_failure_and_legacy_columns_never_raise():
    conn = sqlite3.connect(':memory:')
    assert timers.local(conn, {('s', 'a')}) == {}
    conn.execute('CREATE TABLE tasks (id TEXT, status TEXT, started_at REAL)')
    conn.execute('INSERT INTO tasks VALUES ("a", "running", 100)')
    assert timers.card_timer(timers.local(conn, {('s', 'a')})['a'])['since'] == 100
    conn.close()
