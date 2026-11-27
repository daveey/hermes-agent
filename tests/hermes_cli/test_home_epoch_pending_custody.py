"""Regression contracts for PR4300 review findings 3 and 6.

Ignored duplicate release reservations must not rewrite captured epochs; pending
custody must preserve the entire task row, including local claim bookkeeping.
"""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as dbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_failure_release as release
from hermes_cli import kanban_home_generation as generation
from hermes_cli import kanban_home_transfer as transfer
from hermes_cli import kanban_retain_transport as retain
from tests.hermes_cli import test_home_release_candidates as custody_support
from tests.hermes_cli import test_kanban_failure_transport as failure_support
from tests.hermes_cli import test_kanban_retain_transport as retain_support

live = failure_support.live
OLD_EPOCH = 'committed-nonce-0001'
NEW_EPOCH = 'committed-nonce-0002'


def committed(conn, task, nonce, version):
    with dbc.write_txn(conn):
        generation.mirror_feed(conn, task, {'home_generation': nonce,
                                          'home_transfer_pending': None}, version)
    assert not transfer.frozen(conn, task)


def captured_retain(conn):
    task, run = failure_support._running(conn)
    committed(conn, task, OLD_EPOCH, 8)
    owned = retain_support.reserve(conn, task, run)
    assert json.loads(owned['home_generations']) == {task: OLD_EPOCH}
    return task, run, owned


def test_rejected_retain_reserves_one_release_after_committed_generation(live):
    task, run, owned = captured_retain(live)
    with dbc.write_txn(live):
        retain.record_result(live, {'op_id': owned['op_id']},
                             {'status': 'rejected', 'reason': 'episode_changed'})
    before = retain.command(live, owned['run_uid'])
    task_before = dict(live.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
    committed(live, task, NEW_EPOCH, 9)

    with dbc.write_txn(live):
        assert release.queue(live, task, run, outcome='crashed', error='first failure')
        assert release.queue(live, task, run, outcome='timed_out', error='duplicate failure')

    rows = list(live.execute("SELECT * FROM kanban_sync_outbox WHERE op='release'"))
    assert len(rows) == 1
    row = rows[0]
    assert json.loads(row['home_generations']) == {task: NEW_EPOCH}
    assert json.loads(row['fields'])['outcome'] == 'crashed'
    after = retain.command(live, owned['run_uid'])
    assert after == {**before, 'rejected_release_op_id': row['op_id']}
    assert json.loads(after['home_generations']) == {task: OLD_EPOCH}
    assert dict(live.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()) == task_before
    with dbc.write_txn(live):
        live.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (row['op_id'],))
        assert release.queue(live, task, run, outcome='crashed', error='after ACK')
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE op='release'").fetchone()
    assert retain.command(live, owned['run_uid']) == after


def test_mark_sent_duplicate_is_noop_for_captured_retain(live):
    task, _run, owned = captured_retain(live)
    outbox_before = [dict(row) for row in live.execute('SELECT * FROM kanban_sync_outbox')]
    with dbc.write_txn(live):
        release.mark_sent(live, owned['run_uid'], task)
    assert retain.command(live, owned['run_uid']) == owned
    assert [dict(row) for row in live.execute('SELECT * FROM kanban_sync_outbox')] == outbox_before


@pytest.mark.parametrize('epoch', [None, NEW_EPOCH])
def test_insert_or_ignore_preserves_existing_command_epoch(live, epoch):
    task, _run, owned = captured_retain(live)
    raw = None if epoch is None else json.dumps({task: epoch}, separators=(',', ':'))
    with dbc.write_txn(live):
        count = live.execute('INSERT OR IGNORE INTO kanban_failure_commands '
                             '(run_uid,task_id,op_id,operation_kind,home_generations) '
                             "VALUES (?,?,?,'release',?)",
                             (owned['run_uid'], task, 'ignored-release', raw)).rowcount
        assert count == 0
    assert retain.command(live, owned['run_uid']) == owned


@pytest.mark.parametrize('operation', ['update', 'replace'])
@pytest.mark.parametrize('epoch', [None, NEW_EPOCH])
def test_real_epoch_rewrite_is_refused(live, operation, epoch):
    task, _run, owned = captured_retain(live)
    raw = None if epoch is None else json.dumps({task: epoch}, separators=(',', ':'))
    with pytest.raises(sqlite3.IntegrityError, match='captured command home generation is immutable'):
        with dbc.write_txn(live):
            if operation == 'update':
                live.execute('UPDATE kanban_failure_commands SET home_generations=? WHERE run_uid=?',
                             (raw, owned['run_uid']))
            else:
                live.execute('INSERT OR REPLACE INTO kanban_failure_commands '
                             '(run_uid,task_id,op_id,operation_kind,home_generations) '
                             "VALUES (?,?,?,'release',?)",
                             (owned['run_uid'], task, 'replacement-release', raw))
    assert retain.command(live, owned['run_uid']) == owned


def test_changed_retain_payload_still_refuses_after_committed_generation(live):
    _task, run, owned = captured_retain(live)
    with pytest.raises(ValueError, match='changed'):
        retain_support.reserve(live, owned['task_id'], run, summary='Different semantic intent')
    assert retain.command(live, owned['run_uid']) == owned


TASK_WRITES = [('worker_pid', 987654), ('claim_lock', 'fixture:claim'),
               ('status', 'running'), ('title', 'changed under custody'),
               ('assignee', 'different-worker')]


@pytest.mark.parametrize('column,value', TASK_WRITES)
@pytest.mark.parametrize('phase', ['prepared', 'pending'])
def test_custody_refuses_task_row_writes_including_local_claim_fields(live, phase, column, value):
    task = kb.create_task(live, title='custody fixture', assignee='worker')
    custody_support.freeze(live, task, phase)
    assert transfer.frozen(live, task)
    before = dict(live.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
    assert before[column] != value
    outbox_before = [dict(row) for row in live.execute('SELECT * FROM kanban_sync_outbox')]
    with pytest.raises(sqlite3.IntegrityError, match=f'home transfer {phase}'):
        with dbc.write_txn(live):
            live.execute(f'UPDATE tasks SET {column}=? WHERE id=?', (value, task))
    assert dict(live.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()) == before
    assert [dict(row) for row in live.execute('SELECT * FROM kanban_sync_outbox')] == outbox_before
    assert transfer.frozen(live, task)


@pytest.mark.parametrize('column,value', TASK_WRITES)
def test_committed_generation_allows_ordinary_task_row_writes(live, column, value):
    task = kb.create_task(live, title='unfrozen control', assignee='worker')
    committed(live, task, OLD_EPOCH, 8)
    with dbc.write_txn(live):
        live.execute(f'UPDATE tasks SET {column}=? WHERE id=?', (value, task))
    row = dict(live.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
    assert row[column] == value
    assert not transfer.frozen(live, task)


def test_old_ignore_aborting_trigger_is_upgraded_on_reinstall(live):
    from hermes_cli import kanban_home_command as command
    task, _run, owned = captured_retain(live)
    with dbc.write_txn(live):
        for name in ('home_epoch1_kanban_failure_commands_insert',
                     'home_epoch1_kanban_failure_commands_after'):
            live.execute(f'DROP TRIGGER {name}')
        live.execute("CREATE TRIGGER home_epoch1_kanban_failure_commands_insert BEFORE INSERT ON "
                     "kanban_failure_commands WHEN EXISTS(SELECT 1 FROM kanban_failure_commands "
                     "WHERE run_uid=NEW.run_uid AND home_generations IS NOT NEW.home_generations) "
                     "BEGIN SELECT RAISE(ABORT,'captured command home generation is immutable'); END")
        command.install(live, 'kanban_failure_commands')
        command.install(live, 'kanban_failure_commands')  # idempotent reconnect
        assert live.execute('INSERT OR IGNORE INTO kanban_failure_commands '
                            '(run_uid,task_id,op_id,operation_kind,home_generations) '
                            "VALUES (?,?,'ignored','release',NULL)",
                            (owned['run_uid'], task)).rowcount == 0
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        with dbc.write_txn(live):
            live.execute('INSERT OR REPLACE INTO kanban_failure_commands '
                         '(run_uid,task_id,op_id,operation_kind,home_generations) '
                         "VALUES (?,?,'replace','release',NULL)", (owned['run_uid'], task))
    assert retain.command(live, owned['run_uid']) == owned


def test_ignored_insert_leaves_no_stale_memo_that_blesses_later_replace(live):
    task, _run, owned = captured_retain(live)
    raw = json.dumps({task: NEW_EPOCH}, separators=(',', ':'))
    with dbc.write_txn(live):
        live.execute('INSERT OR IGNORE INTO kanban_failure_commands '
                     '(run_uid,task_id,op_id,operation_kind,home_generations) '
                     "VALUES (?,?,'ignored','release',?)", (owned['run_uid'], task, raw))
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        with dbc.write_txn(live):
            live.execute('INSERT OR REPLACE INTO kanban_failure_commands '
                         '(run_uid,task_id,op_id,operation_kind,home_generations) '
                         "VALUES (?,?,'replace','release',?)", (owned['run_uid'], task, raw))
    assert retain.command(live, owned['run_uid']) == owned


def test_replace_through_secondary_unique_identity_is_refused(live):
    _task, _run, owned = captured_retain(live)
    from hermes_cli import kanban_home_command as command
    with dbc.write_txn(live):
        live.execute('CREATE UNIQUE INDEX fixture_secondary_op ON kanban_failure_commands(op_id)')
        for suffix in ('insert', 'after'):
            live.execute(f'DROP TRIGGER home_epoch1_kanban_failure_commands_{suffix}')
        command.install(live, 'kanban_failure_commands')
    row = dict(live.execute('SELECT * FROM kanban_failure_commands WHERE run_uid=?',
                            (owned['run_uid'],)).fetchone())
    row.update(run_uid='other-run-uid', home_generations=None)
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        with dbc.write_txn(live):
            live.execute(f'INSERT OR REPLACE INTO kanban_failure_commands ({",".join(row)}) '
                         f'VALUES ({",".join("?" * len(row))})', tuple(row.values()))
    assert retain.command(live, owned['run_uid']) == owned


def test_pending_fence_survives_reconnect_and_board_apply_is_exempt(live):
    task = kb.create_task(live, title='reconnect fixture', assignee='worker')
    custody_support.freeze(live, task, 'pending')
    with dbc.write_txn(live):
        generation.install(live)  # reconnect/upgrade reinstall is idempotent
    with pytest.raises(sqlite3.IntegrityError, match='home transfer pending'):
        with dbc.write_txn(live):
            live.execute('UPDATE tasks SET worker_pid=? WHERE id=?', (4242, task))
    with dbc.write_txn(live):
        live.execute('UPDATE kanban_sync_state SET applying=1')
        live.execute('UPDATE tasks SET worker_pid=? WHERE id=?', (4242, task))
        live.execute('UPDATE kanban_sync_state SET applying=0')
    assert live.execute('SELECT worker_pid FROM tasks WHERE id=?', (task,)).fetchone()[0] == 4242
