"""Rejected intent is not an applied retain or a replica mutation. [daveey]"""
import hashlib
import json
import sqlite3

import pytest

from hermes_cli import kanban_sync_retain_drift as drift


@pytest.fixture
def stores():
    live, shadow = (sqlite3.connect(':memory:') for _ in range(2))
    for conn in (live, shadow):
        conn.row_factory = sqlite3.Row
        conn.executescript('''
            CREATE TABLE tasks(id TEXT, current_run_id INTEGER);
            CREATE TABLE task_runs(id INTEGER, uid TEXT, task_id TEXT, ended_at INTEGER);
            INSERT INTO tasks VALUES ('t_card', NULL);
            INSERT INTO task_runs VALUES (1, 'run', 't_card', 123);
        ''')
    live.execute('''CREATE TABLE kanban_failure_commands(
        run_uid TEXT, task_id TEXT, op_id TEXT, operation_kind TEXT,
        request_uid TEXT, event_uid TEXT, request_json TEXT, request_sha256 TEXT,
        delivery_state TEXT, canonical_receipt_json TEXT)''')
    wire = dict(op='retain', client_op_id='op', task='t_card', run_uid='run', event_uid='event')
    text = json.dumps(wire, sort_keys=True, separators=(',', ':'))
    live.execute('INSERT INTO kanban_failure_commands VALUES (?,?,?,?,?,?,?,?,?,?)',
                 ('run', 't_card', 'op', 'retain', 'op', 'event', text,
                  hashlib.sha256(text.encode()).hexdigest(), 'rejected', None))
    op = dict(op_id='op', row_uid='t_card', table_name='tasks', op='retain',
              state='rejected', last_error='missing_decision_episode', fields=text)
    yield live, shadow, op
    live.close()
    shadow.close()


def test_exact_rejection_is_diagnostic_only(stores):
    live, shadow, op = stores
    before = list(live.iterdump())
    assert drift.audited_rejection(live, shadow, op)
    assert list(live.iterdump()) == before


@pytest.mark.parametrize('change', [
    'unknown', 'receipt', 'digest', 'wire', 'request', 'event', 'task',
    'live_run', 'shadow_run', 'active', 'shadow_active', 'reason', 'operation', 'missing_custody',
])
def test_incomplete_or_conflicting_proof_fails_closed(stores, change):
    live, shadow, op = stores
    sql_changes = {
        'unknown': (live, "UPDATE kanban_failure_commands SET delivery_state='unknown'"),
        'receipt': (live, "UPDATE kanban_failure_commands SET canonical_receipt_json='{}'"),
        'digest': (live, "UPDATE kanban_failure_commands SET request_sha256='bad'"),
        'request': (live, "UPDATE kanban_failure_commands SET request_uid='other'"),
        'event': (live, "UPDATE kanban_failure_commands SET event_uid='other'"),
        'task': (live, "UPDATE kanban_failure_commands SET task_id='other'"),
        'live_run': (live, 'UPDATE task_runs SET ended_at=NULL'),
        'shadow_run': (shadow, 'UPDATE task_runs SET ended_at=NULL'),
        'active': (live, 'UPDATE tasks SET current_run_id=1'),
        'shadow_active': (shadow, 'UPDATE tasks SET current_run_id=1'),
        'missing_custody': (live, 'DROP TABLE kanban_failure_commands'),
    }
    op_changes = {'wire': {'fields': op['fields'].replace('event', 'other')},
                  'reason': {'last_error': 'storage_error'}, 'operation': {'op': 'release'}}
    if change in sql_changes:
        conn, sql = sql_changes[change]
        conn.execute(sql)
    else:
        op.update(op_changes[change])
    assert not drift.audited_rejection(live, shadow, op)


def test_diff_preserves_audits_and_real_drift(stores):
    from hermes_cli.kanban_sync_client import diff

    live, shadow, op = stores
    for conn in (live, shadow):
        conn.executescript('''
            ALTER TABLE tasks ADD COLUMN sync_version INTEGER;
            ALTER TABLE tasks ADD COLUMN created_on TEXT;
            ALTER TABLE tasks ADD COLUMN title TEXT DEFAULT 'same';
            CREATE TABLE task_links(parent_id TEXT, child_id TEXT);
            CREATE TABLE task_comments(task_id TEXT, uid TEXT);
            CREATE TABLE task_chat_links(task_id TEXT, uid TEXT, chat_gateway TEXT);
        ''')
    live.execute('''CREATE TABLE kanban_sync_outbox(op_id TEXT, row_uid TEXT, table_name TEXT,
                    op TEXT, state TEXT, last_error TEXT, fields TEXT)''')
    live.execute('INSERT INTO kanban_sync_outbox VALUES (?,?,?,?,?,?,?)',
                 tuple(op[k] for k in ('op_id', 'row_uid', 'table_name', 'op', 'state', 'last_error', 'fields')))
    before = list(live.iterdump())
    report = diff(live, shadow, 'macbook')
    assert report['clean'], report
    assert report['rejected_ops'] == 1
    assert report['audited_retain_rejections'] == ['op']
    assert list(live.iterdump()) == before
    shadow.execute("UPDATE tasks SET title='different'")
    assert not diff(live, shadow, 'macbook')['clean']
    shadow.execute("UPDATE tasks SET title='same'")
    live.execute("INSERT INTO kanban_sync_outbox VALUES ('other','t_card','tasks','upsert','rejected','conflict','{}')")
    assert not diff(live, shadow, 'macbook')['clean']
