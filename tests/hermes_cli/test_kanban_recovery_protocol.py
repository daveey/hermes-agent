"""Old indexed keys do not authenticate current protocol custody after table damage."""
import shutil
import sqlite3

import pytest

from hermes_cli import kanban_db_recover as recovery
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_replay_deferred as deferred
from hermes_cli import kanban_replay_observation as observation
from hermes_cli import kanban_replay_episodes as episodes
from hermes_cli import kanban_replay_diagnostics as diagnostics
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_replay_existence as existence
from hermes_cli import kanban_actionability_transport as actionability
from hermes_cli import kanban_failure_release as failure
from hermes_cli import kanban_sync_reserved_key as reserved
from tests.hermes_cli import test_kanban_db_recover as damage

# Real durable schemas and their non-indexed mutable history; not a command-API fixture.
CASES = [
    ('kanban_replay_deferred1', 'capture_token'),
    ('kanban_replay_observation1', 'evidence_sha256'),
    ('kanban_replay_observation_reads1', 'token'),
    ('kanban_replay_episode1', 'sample_token'),
    ('kanban_replay_diagnostic1', 'receipt_json'),
    ('kanban_failure_commands', 'canonical_receipt_json'),
    ('kanban_actionability_commands', 'canonical_receipt_json'),
    ('kanban_sync_omissions', 'restore_op_id'),
    ('kanban_sync_status_origins', 'origin'),
    ('kanban_sync_key_supersessions', 'plan_token'),
    ('kanban_sync_key_supersession_ops', 'op_json'),
    ('kanban_sync_key_supersession_children', 'receipt_id'),
]


def install(conn):
    conn.executescript(sync.SYNC_SCHEMA_SQL)
    for sql in (journal._SCHEMA, observation._SCHEMA, observation._CURSOR, observation._READS,
                episodes._SCHEMA, episodes._CLOCK, existence._SCHEMA, diagnostics._SCHEMA):
        conn.execute(sql)
    deferred.install(conn)
    failure._ensure_commands(conn)
    actionability.ensure_schema(conn)
    reserved.ensure_schema(conn)


@pytest.mark.parametrize('table,mutable', CASES)
def test_damaged_protocol_rows_are_not_refilled_from_stale_index_matching_backup(tmp_path, table, mutable):
    path, backup, rebuilt = (tmp_path / name for name in ('current.db', 'old.bak', 'rebuilt.db'))
    with sqlite3.connect(path) as conn:
        install(conn)
        columns = list(conn.execute(f'PRAGMA table_info({table})'))
        for i in range(100):
            # Populate storage directly: all indexed keys stay equal across the backup;
            # only a non-key custody value changes. No business execution is asserted.
            values = [i if 'INT' in col[2].upper() else f'{col[1]}-{i:03}-' + 'x' * 80 for col in columns]
            conn.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in values)})", values)
        conn.execute(f'UPDATE {table} SET {mutable}=?', ('old-custody',))
    shutil.copy2(path, backup)
    with sqlite3.connect(path) as conn:
        conn.execute(f'UPDATE {table} SET {mutable}=?', ('current-custody',))
        before = {row[0]: tuple(row[1:]) for row in conn.execute(f'SELECT rowid,* FROM {table}')}
    lost = damage._damage_table(path, table)
    assert lost, 'fixture must actually destroy protocol rows'
    report = recovery.RecoveryResult(status='building', db_path=str(path))
    recovery._build(path, rebuilt, backup, report)
    with sqlite3.connect(rebuilt) as dst:
        after = {row[0]: tuple(row[1:]) for row in dst.execute(f'SELECT rowid,* FROM {table}')}
    loss = report.tables[table]
    assert loss.filled_from_backup == 0, 'indexed identity resurrected stale protocol custody'
    assert loss.known_lost == len(lost)
    assert after == {key: value for key, value in before.items() if key not in lost}
    value_at = next(index for index, col in enumerate(columns) if col[1] == mutable)
    assert {row[value_at] for row in after.values()} == {'current-custody'}


@pytest.mark.parametrize('table', [
    'kanban_replay_sender1', 'kanban_replay_observation_cursor1',
    'kanban_replay_episode_clock1', 'kanban_replay_existence_fault1',
])
def test_current_singleton_schemas_have_no_backup_listing_path(tmp_path, table):
    path, rebuilt = tmp_path / 'singleton.db', tmp_path / 'rebuilt.db'
    with sqlite3.connect(path) as conn:
        install(conn)
        assert list(conn.execute(f'PRAGMA index_list({table})')) == []
        # A valid one-row storage image. These are real schemas with CHECK(id=1).
        columns = list(conn.execute(f'PRAGMA table_info({table})'))
        values = [1 if 'INT' in col[2].upper() else 'retained-current' for col in columns]
        conn.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in values)})", values)
        before = conn.execute(f'SELECT * FROM {table}').fetchall()
    report = recovery.RecoveryResult(status='building', db_path=str(path))
    recovery._build(path, rebuilt, path, report)
    with sqlite3.connect(rebuilt) as dst:
        assert dst.execute(f'SELECT * FROM {table}').fetchall() == before
    assert report.tables[table].recovered == 1
    assert report.tables[table].filled_from_backup == 0
