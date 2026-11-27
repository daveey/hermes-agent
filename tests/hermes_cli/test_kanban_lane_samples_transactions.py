"""Sampler failures must not leak transaction authority into real dispatch. [daveey]"""
import copy
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_lane_samples as samples, kanban_lanes as lanes
from hermes_cli import kanban_candidate_receipts as receipts
from hermes_cli.kanban_db_dispatch import dispatch_once
from tests.hermes_cli import test_kanban_lanes as base

conn = base.conn
configured = base.configured
person = base.person


def _record(conn):
    pol = lanes.policy()
    samples.record(conn, pol, dict.fromkeys(pol.lanes, 0), samples.Capture(pol.lanes), now=60)


def test_dispatch_recovers_from_real_wal_outermost_release_failure(conn, configured, caplog):
    """Deferred FKs fail at outer RELEASE (not INSERT), leaving a live WAL transaction."""
    assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert conn.execute('PRAGMA database_list').fetchone()[2]  # file-backed, not :memory:
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    mapping = copy.deepcopy(base.LANES)
    mapping['babybear']['max_workers'] = 1
    configured(mapping)
    running = base._ready_in(conn, 'babybear', 'running')
    assert kb.claim_task(conn, running)
    waiting = base._ready_in(conn, 'babybear', 'waiting')
    conn.execute(samples.SCHEMA)
    conn.execute('CREATE TABLE sample_parent (id INTEGER PRIMARY KEY)')
    conn.execute('CREATE TABLE sample_child (parent INTEGER REFERENCES sample_parent(id) '
                 'DEFERRABLE INITIALLY DEFERRED)')
    conn.execute('CREATE TRIGGER fail_sample_commit AFTER INSERT ON lane_samples BEGIN '
                 'INSERT INTO sample_child VALUES (1); END')
    conn.commit()

    # Prove the real engine fault's timing and state independently of sampler cleanup.
    conn.execute('SAVEPOINT engine_control')
    conn.execute('INSERT INTO sample_child VALUES (1)')
    with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY constraint failed'):
        conn.execute('RELEASE engine_control')
    assert conn.in_transaction
    conn.rollback()

    result = dispatch_once(conn, spawn_fn=lambda *_a, **_k: pytest.fail('lane cap must prevent spawn'),
                           max_spawn=2, max_in_progress=10, reconcile_orphans=False)
    assert not result.spawned
    assert kb.get_task(conn, waiting).status == 'ready'
    assert kb.get_task(conn, running).status == 'running'
    assert not conn.in_transaction
    assert conn.execute('SELECT count(*) FROM lane_samples').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM sample_child').fetchone()[0] == 0
    assert conn.execute(f'SELECT count(*) FROM {receipts.PASSES}').fetchone()[0] == 1
    assert 'lane occupancy sample unavailable' in caplog.text
    assert 'FOREIGN KEY constraint failed' in caplog.text
    # A following dispatch can commit both samples and receipts after fault removal.
    conn.execute('DROP TRIGGER fail_sample_commit')
    conn.commit()
    dispatch_once(conn, spawn_fn=lambda *_a, **_k: pytest.fail('lane cap must prevent spawn'),
                  max_spawn=2, max_in_progress=10, reconcile_orphans=False)
    assert not conn.in_transaction
    assert conn.execute('SELECT count(*) FROM lane_samples').fetchone()[0] == len(lanes.policy().lanes)
    assert conn.execute(f'SELECT count(*) FROM {receipts.PASSES}').fetchone()[0] == 2


@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('action', ['ABORT', 'ROLLBACK'])
def test_statement_failure_preserves_original_error_and_caller_authority(conn, configured, nested, action):
    conn.execute(samples.SCHEMA)
    conn.execute('CREATE TABLE caller_marker (value)')
    conn.execute(f"CREATE TRIGGER fail_sample_insert BEFORE INSERT ON lane_samples BEGIN "
                 f"SELECT RAISE({action}, 'sample insert failed'); END")
    conn.commit()
    if nested:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO caller_marker VALUES (1)')
    with pytest.raises(sqlite3.IntegrityError, match='^sample insert failed$'):
        _record(conn)
    caller_survives = nested and action == 'ABORT'
    assert conn.in_transaction == caller_survives
    assert conn.execute('SELECT count(*) FROM caller_marker').fetchone()[0] == int(caller_survives)
    assert conn.execute('SELECT count(*) FROM lane_samples').fetchone()[0] == 0
    if caller_survives:
        conn.commit()
        assert conn.execute('SELECT count(*) FROM caller_marker').fetchone()[0] == 1
    assert not conn.in_transaction


def test_denied_release_cleanup_rolls_back_only_live_transaction(conn, configured):
    """SQLite authorizer denies both RELEASE attempts; ROLLBACK must close the unit."""
    assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    rejected_while_active = []

    def deny_release(action, operation, name, _database, _trigger):
        if action == sqlite3.SQLITE_SAVEPOINT and operation == 'RELEASE' and name == 'lane_samples_record':
            rejected_while_active.append(conn.in_transaction)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_release)
    try:
        with pytest.raises(sqlite3.DatabaseError, match='not authorized'):
            _record(conn)
    finally:
        conn.set_authorizer(None)
    assert rejected_while_active == [True, True]
    assert not conn.in_transaction
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='lane_samples'").fetchone()
    _record(conn)
    assert not conn.in_transaction


def test_unexpected_sample_error_is_logged_without_escaping_dispatch(conn, configured, monkeypatch, caplog):
    def fail_record(*_args, **_kwargs):
        raise RuntimeError('sampler unavailable')

    monkeypatch.setattr(samples, 'record', fail_record)
    result = dispatch_once(conn, spawn_fn=lambda *_a, **_k: pytest.fail('empty board'),
                           max_spawn=2, max_in_progress=10, reconcile_orphans=False)
    assert not result.spawned
    assert not conn.in_transaction
    assert conn.execute(f'SELECT count(*) FROM {receipts.PASSES}').fetchone()[0] == 1
    assert 'lane occupancy sample unavailable' in caplog.text
    assert 'sampler unavailable' in caplog.text
