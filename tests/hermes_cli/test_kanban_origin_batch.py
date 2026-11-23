"""Batching changes SQL crossings, never exact custody or freshness decisions."""
import sqlite3

import pytest

from hermes_cli import kanban_origin_batch as batch, kanban_origin_ledger as ledger
from tests.hermes_cli import test_kanban_origin_ledger as support

world = support.world


def test_exact_keys_dedup_absence_failure_and_rollback(monkeypatch):
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE replica(gw,coll,key,version,data,PRIMARY KEY(gw,coll,key))')
    conn.executemany('INSERT INTO replica VALUES(?,?,?,?,?)', [
        ('owner', 'tasks', 'default/default/t_one', 2, '{}'),
        ('other', 'tasks', 'default/default/t_one', 8, 'wrong'),
        ('owner', 'tasks', 'other/default/t_one', 9, 'wrong')])
    bindings = [{'owner': {'gateway': 'owner'}, 'task_record_key': 'default/default/t_one'},
                {'owner': {'gateway': 'missing'}, 'task_record_key': 'default/default/t_one'}]
    sql = []
    conn.set_trace_callback(sql.append)
    rows = batch.task_rows(conn, bindings * 100)
    assert rows == {('owner', 'default/default/t_one'): (2, '{}')}
    assert len(sql) == 1
    assert batch.lookup(conn, bindings[1], rows) is None
    assert len(sql) == 1
    monkeypatch.setenv('HERMES_ORIGIN_BATCH_READS', '0')
    assert batch.task_rows(conn, bindings) is None
    assert batch.lookup(conn, bindings[0], None) == (2, '{}')
    monkeypatch.delenv('HERMES_ORIGIN_BATCH_READS')
    conn.execute('DROP TABLE replica')
    with pytest.raises(sqlite3.OperationalError):
        batch.task_rows(conn, bindings)
    conn.close()


@pytest.mark.parametrize('enabled', ['0', '1'])
def test_refresh_and_projection_remain_strict_after_committed_changes(world, monkeypatch, enabled):
    monkeypatch.setenv('HERMES_ORIGIN_BATCH_READS', enabled)
    path, replica, binding = world
    support.publish(replica, binding)
    support.accept(world)
    for status, version in [('done', 4), ('running', 5)]:
        support.publish(replica, binding, status=status)
        with sqlite3.connect(replica) as db:
            db.execute('UPDATE replica SET version=?', (version,))
        ledger.refresh_observations(path, replica)
        row = ledger.obligations(path, gateway='origin', profile='default', sessions=['s_current'])[0]
        assert row['observation_version'] == version
        assert row['unfinished']  # unverified terminal evidence is never upgraded
        projected = ledger.project(path, replica=replica, gateway='origin', profile='default')
        assert projected[0]['reported_status'] == status
        assert projected[0]['observation_version'] == version
        assert projected[0]['status'] == 'unknown'  # fixture owner has no current scope heartbeat


def test_batch_work_is_bounded_by_requested_keys_not_replica_inventory(monkeypatch):
    monkeypatch.setenv('HERMES_ORIGIN_BATCH_READS', '1')
    conn = sqlite3.connect(':memory:')
    try:
        conn.execute('CREATE TABLE replica(gw TEXT,coll TEXT,key TEXT,version INTEGER,data TEXT,'
                     'PRIMARY KEY(gw,coll,key))')
        # Production secondary indexes let SQLite reorder an ordinary JOIN to
        # scan all task records, then scan json_each once for every record.
        conn.execute('CREATE INDEX replica_poll_metadata ON replica(coll,gw)')
        conn.executemany('INSERT INTO replica VALUES(?,?,?,?,?)',
                         [('owner', 'tasks', str(i), i, '{}') for i in range(3000)])
        bindings = [{'owner': {'gateway': 'owner'}, 'task_record_key': str(i)} for i in range(8)]
        calls = []
        def budget():
            calls.append(1)
            return len(calls) > 10
        conn.set_progress_handler(budget, 1000)
        assert batch.task_rows(conn, bindings) == {('owner', str(i)): (i, '{}') for i in range(8)}
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


def test_locked_refresh_takes_new_batch_after_preflight(world, monkeypatch):
    path, replica, binding = world
    support.publish(replica, binding)
    support.accept(world)
    support.publish(replica, binding, status='done')
    with sqlite3.connect(replica) as db:
        db.execute('UPDATE replica SET version=4')
    original = batch.task_rows
    phases = []
    def rows(source, bindings):
        result = original(source, bindings)
        phases.append(result)
        if len(phases) == 1:
            support.publish(replica, binding, status='running')
            with sqlite3.connect(replica) as db:
                db.execute('UPDATE replica SET version=5')
        return result
    monkeypatch.setattr(batch, 'task_rows', rows)
    ledger.refresh_observations(path, replica)
    assert len(phases) == 2
    assert next(iter(phases[0].values()))[0] == 4
    assert next(iter(phases[1].values()))[0] == 5
    row = ledger.obligations(path, gateway='origin', profile='default', sessions=['s_current'])[0]
    assert row['observation_version'] == 5 and row['unfinished']
