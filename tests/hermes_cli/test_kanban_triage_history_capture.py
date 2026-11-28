"""The session reader must not hydrate overlapping triage histories twice."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_retained_status as retained
from hermes_cli import kanban_triage_attention as attention


def test_history_rows_cross_snapshot_boundary_once():
    seen = []

    def record(cursor, values):
        names = [item[0] for item in cursor.description]
        if 'payload' in names and 'task_id' in names:
            seen.append(values[names.index('id')])
        return sqlite3.Row(cursor, values)

    with sqlite3.connect(':memory:') as conn:
        conn.executescript('''
            CREATE TABLE tasks (id TEXT, current_run_id, claim_lock, claim_expires, human_request);
            CREATE TABLE task_runs (task_id TEXT, status TEXT, ended_at);
            CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id, kind TEXT,
                                      payload TEXT, created_at INTEGER, uid TEXT);
            INSERT INTO tasks VALUES ('card', NULL, NULL, NULL, NULL);
        ''')
        payload = {'disposition': {'reason': 'scope_stopped', 'summary': 'stopped'}, 'episode_uid': 'episode'}
        events = [('created', {'status': 'ready'}, 1), ('contained', {}, 2),
                  ('decision_retained', payload, 3)]
        conn.executemany('INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)',
                         [('card', kind, json.dumps(body), at) for kind, body, at in events])
        conn.row_factory = record
        conn.execute('BEGIN') if not conn.in_transaction else None
        rows = attention.annotate(conn, [{'id': 'card', 'status': 'triage'}])
        assert conn.in_transaction
        assert rows == [{'id': 'card', 'status': 'triage', 'triage_entered_at': 2,
                         'retained_stop': {'episode_uid': 'episode', 'reason': 'scope_stopped',
                                           'summary': 'stopped'}}]
        assert len(seen) == len(set(seen)), f'duplicate history hydration inside snapshot: {seen}'


@pytest.mark.parametrize('kind', sorted(set(attention.LIFECYCLE_KINDS) | retained._RELEVANT))
@pytest.mark.parametrize('legacy', [False, True])
def test_shared_capture_matches_separate_reducers_for_every_kind(kind, legacy):
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript('''
            CREATE TABLE tasks (id TEXT, current_run_id, claim_lock, claim_expires, human_request);
            CREATE TABLE task_runs (task_id TEXT, status TEXT, ended_at);
            CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id, kind TEXT,
                                      payload TEXT, created_at INTEGER);
            INSERT INTO tasks VALUES ('card', NULL, NULL, NULL, NULL);
        ''')
        if not legacy:
            conn.execute('ALTER TABLE task_events ADD COLUMN uid TEXT')
        stop = {'disposition': {'reason': 'scope_stopped', 'summary': 'hold'}, 'episode_uid': 'episode'}
        history = [('created', {'status': 'ready'}, 1), ('contained', {}, 2),
                   ('decision_retained', stop, 3), (kind, {}, 4)]
        conn.executemany('INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)',
                         [('card', name, json.dumps(body), at) for name, body, at in history])
        expected = [{'id': 'card', 'status': 'triage', 'retained_stop': {'old': True}}]
        retained.annotate(conn, expected)
        lifecycle = [dict(row) for row in conn.execute('SELECT * FROM task_events ORDER BY id')
                     if row['kind'] in attention.LIFECYCLE_KINDS]
        expected[0]['triage_entered_at'] = attention.current_entry({'status': 'triage', 'events': lifecycle})
        actual = attention.annotate(conn, [{'id': 'card', 'status': 'triage', 'retained_stop': {'old': True}}])
        assert actual == expected

