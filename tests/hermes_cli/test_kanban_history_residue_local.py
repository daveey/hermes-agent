"""Local historical audit inputs must remain exact, inert and portable."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_history_residue_local as local
from hermes_cli.kanban_history_residue_source import Refused
from tests.hermes_cli.test_kanban_history_residue_source import specimen


@pytest.fixture
def stores():
    source = specimen()
    raw = json.dumps(source)
    dbs = [sqlite3.connect(':memory:', isolation_level=None) for _ in range(2)]
    for conn in dbs:
        conn.row_factory = sqlite3.Row
        cols = sync.SYNCED_COLUMNS['tasks'] | sync.SERVER_COLUMNS['tasks'] | sync.LOCAL_COLUMNS['tasks'] | {'id'}
        conn.execute('CREATE TABLE tasks (' + ','.join(f'"{c}"' for c in sorted(cols)) + ')')
        conn.executescript('''
            CREATE TABLE task_events(id INTEGER,uid TEXT,task_id TEXT,run_id INTEGER,kind TEXT,payload TEXT,created_at INTEGER);
            CREATE TABLE task_runs(id INTEGER,uid TEXT,task_id TEXT,status TEXT,ended_at INTEGER);
            CREATE TABLE task_links(parent_id TEXT,child_id TEXT);
            CREATE TABLE task_comments(task_id TEXT,uid TEXT);
            CREATE TABLE task_chat_links(task_id TEXT,uid TEXT,chat_gateway TEXT);
            CREATE TABLE kanban_sync_state(board_id TEXT,gateway_id TEXT,mode TEXT,cursor INTEGER,applying INTEGER);
            INSERT INTO kanban_sync_state VALUES ('default','home','primary',100,0);
            CREATE TABLE kanban_sync_tombstones(table_name TEXT,row_uid TEXT,deleted_at INTEGER);
            CREATE TABLE kanban_sync_omissions(table_name TEXT,row_uid TEXT,field TEXT,state TEXT);
        ''')
        conn.execute('CREATE TABLE kanban_sync_outbox (' + ','.join(f'"{c}"' for c in source) + ')')
        conn.execute("INSERT INTO tasks(id,title,status,created_on,sync_version) VALUES ('t_stopped','same','triage','home',7)")
    live, shadow = dbs
    live.execute('INSERT INTO kanban_sync_outbox VALUES (' + ','.join('?' for _ in source) + ')', tuple(source.values()))
    fields = json.loads(source['fields'])
    live.execute('INSERT INTO task_events VALUES (1,?,?,?,?,?,?)',
                 (source['row_uid'], fields['task_id'], None, fields['kind'], fields['payload'], fields['created_at']))
    shadow.execute("UPDATE kanban_sync_state SET mode='shadow'")
    try:
        yield live, shadow, raw
    finally:
        for conn in dbs:
            conn.close()


def test_snapshot_preserves_local_ids_and_exact_full_op_without_writes(stores):
    live, shadow, raw = stores
    before = [list(c.iterdump()) for c in (live, shadow)]
    a = local.snapshot(live, raw, board='default', gateway='home', replica=False)
    b = local.snapshot(shadow, raw, board='default', gateway='home', replica=True)
    assert a['portable'] == b['portable']
    assert a['portable']['current_run_uid'] is None
    assert a['portable']['version'] == 7
    assert a['event'][0]['id'] == 1 and b['event'] == []
    assert before == [list(c.iterdump()) for c in (live, shadow)]


@pytest.mark.parametrize('fault', ['op-bytes','attempts','event-bytes','event-run','missing-event','extra-op',
    'pending-parent','tombstone','omission','claim','worker','lease','pointer','open-run','status','owner',
    'version','applying','identity','missing-schema','uncovered-op'])
def test_local_vetoes(stores, fault):
    live, _shadow, raw = stores
    changes = {
        'op-bytes': "UPDATE kanban_sync_outbox SET wire_json=wire_json || ' '",
        'attempts': 'UPDATE kanban_sync_outbox SET attempts=3',
        'event-bytes': "UPDATE task_events SET payload='changed'",
        'event-run': 'UPDATE task_events SET run_id=33',
        'missing-event': 'DELETE FROM task_events',
        'extra-op': "INSERT INTO kanban_sync_outbox SELECT 'other',table_name,row_uid,op,fields,wire_json,hlc,state,attempts,identity_state,last_error FROM kanban_sync_outbox",
        'pending-parent': "INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,state) VALUES ('new','tasks','t_stopped','pending')",
        'tombstone': "INSERT INTO kanban_sync_tombstones VALUES ('task_events','event-original',2)",
        'omission': "INSERT INTO kanban_sync_omissions VALUES ('tasks','t_stopped','status','held')",
        'claim': "UPDATE tasks SET claim_lock='worker'",
        'worker': 'UPDATE tasks SET worker_pid=4',
        'lease': "UPDATE tasks SET lease_holder='home'",
        'pointer': 'UPDATE tasks SET current_run_id=1',
        'open-run': "INSERT INTO task_runs VALUES (1,'run','t_stopped','running',NULL)",
        'status': "UPDATE tasks SET status='ready'",
        'owner': "UPDATE tasks SET created_on='other'",
        'version': 'UPDATE tasks SET sync_version=NULL',
        'applying': 'UPDATE kanban_sync_state SET applying=1',
        'identity': "UPDATE kanban_sync_state SET board_id='other'",
        'missing-schema': 'DROP TABLE kanban_sync_omissions',
        'uncovered-op': "INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,fields,state) VALUES ('other','task_runs','run','{\"task_id\":\"t_stopped\"}','rejected')",
    }
    live.execute(changes[fault])
    with pytest.raises((Refused, sqlite3.Error)):
        local.snapshot(live, raw, board='default', gateway='home', replica=False)


@pytest.mark.parametrize('fault', ['event','op','tombstone','claim','run'])
def test_replica_is_not_a_second_local_history_custodian(stores, fault):
    _live, shadow, raw = stores
    changes = {
        'event': "INSERT INTO task_events VALUES (9,'event-original','t_stopped',NULL,'promoted_manual','{}',100)",
        'op': "INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,state) VALUES ('other','tasks','t_stopped','pending')",
        'tombstone': "INSERT INTO kanban_sync_tombstones VALUES ('tasks','t_stopped',2)",
        'claim': "UPDATE tasks SET claim_lock='worker'",
        'run': "INSERT INTO task_runs VALUES (1,'run','t_stopped','running',NULL)",
    }
    shadow.execute(changes[fault])
    with pytest.raises(Refused):
        local.snapshot(shadow, raw, board='default', gateway='home', replica=True)


def test_large_cell_bound_precedes_source_materialization(stores, monkeypatch):
    live, _shadow, raw = stores
    live.execute("UPDATE tasks SET body=?", ('x' * 10000,))
    monkeypatch.setattr(local, 'MAX_SNAPSHOT_BYTES', 1024)
    with pytest.raises(Refused, match='local_snapshot_bound'):
        local.snapshot(live, raw, board='default', gateway='home', replica=False)


def test_recently_ended_run_still_part_of_snapshot(stores):
    live, _shadow, raw = stores
    first = local.snapshot(live, raw, board='default', gateway='home', replica=False)
    live.execute("INSERT INTO task_runs VALUES (4,'closed','t_stopped','done',123)")
    assert local.snapshot(live, raw, board='default', gateway='home', replica=False) != first


@pytest.mark.parametrize('replica', [False, True])
@pytest.mark.parametrize('fault', ['tombstone','omission','op'])
def test_run_repair_custody_is_not_hidden_by_terminal_parent(stores, replica, fault):
    live, shadow, raw = stores
    conn = shadow if replica else live
    conn.execute("INSERT INTO task_runs VALUES (4,'closed','t_stopped','done',123)")
    statements = {
        'tombstone': "INSERT INTO kanban_sync_tombstones VALUES ('task_runs','closed',99)",
        'omission': "INSERT INTO kanban_sync_omissions VALUES ('task_runs','closed','ended_at','held')",
        'op': "INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,state) VALUES ('run-delete','task_runs','closed','delete','{}','rejected')",
    }
    conn.execute(statements[fault])
    with pytest.raises(Refused):
        local.snapshot(conn, raw, board='default', gateway='home', replica=replica)
