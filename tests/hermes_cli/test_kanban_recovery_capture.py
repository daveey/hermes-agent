"""A backup's original tuple cannot certify current pending-parent history. [daveey]"""
import json
import shutil
import sqlite3

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_recover as recovery
from hermes_cli import kanban_sync_envelope as envelopes
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli import test_kanban_db_recover as damage


@pytest.mark.parametrize('identity', ['original', 'ambiguous', None])
def test_backup_parent_retains_payload_but_not_originality(tmp_path, monkeypatch, identity):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    path = home / 'kanban.db'
    conn = kbc.connect(path)
    expected = {}
    with kbc.write_txn(conn):
        for i in range(100):
            task = f't_parent_{i:03}'
            wire = json.dumps(dict(client_op_id=f'operation-{i}', table='tasks', uid=task,
                                   op='upsert', fields={'title': 'backup ' + 'x' * 400}))
            capture = ('capture1', f'operation-{i}', 123) if identity else (None, None, None)
            conn.execute('INSERT INTO kanban_sync_parent_envelopes '
                         '(task_id,wire,capture_protocol,original_op_id,original_created_at,identity_state) '
                         'VALUES (?,?,?,?,?,?)', (task, wire, *capture, identity))
            expected[task] = (wire, *capture, identity)
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
    conn.close()
    backup = home / 'before.bak'
    shutil.copy2(path, backup)
    # The current file has a later wire/taint. Its index only proves task identity,
    # not that the backup's original payload and taint are the latest ones.
    current = sqlite3.connect(path)
    current.execute("UPDATE kanban_sync_parent_envelopes SET wire=replace(wire,'backup','latest')")
    current.commit()
    before = dict(current.execute('SELECT task_id,wire FROM kanban_sync_parent_envelopes'))
    rowids = dict(current.execute('SELECT rowid,task_id FROM kanban_sync_parent_envelopes'))
    current.close()
    lost = damage._damage_table(path, 'kanban_sync_parent_envelopes')
    rebuilt = home / 'rebuilt.db'
    result = recovery.RecoveryResult(status='building', db_path=str(path))
    recovery._build(path, rebuilt, backup, result)
    dst = sqlite3.connect(rebuilt)
    try:
        loss = result.tables['kanban_sync_parent_envelopes']
        assert loss.filled_from_backup == len(lost) and loss.known_lost == 0
        restored = {rowids[rid] for rid in lost}
        for task in restored:
            row = dst.execute('SELECT wire,capture_protocol,original_op_id,original_created_at,identity_state '
                              'FROM kanban_sync_parent_envelopes WHERE task_id=?', (task,)).fetchone()
            assert row[:4] == expected[task][:4]
            assert row[4] == 'ambiguous', 'backup recovery resurrected an uncertified original claim'
            assert envelopes.parent(dst, task, lambda: pytest.fail('must retain available custody')) == json.loads(row[0])
            with pytest.raises(sqlite3.IntegrityError, match='capture provenance is immutable'):
                dst.execute("UPDATE kanban_sync_parent_envelopes SET identity_state='original' WHERE task_id=?", (task,))
        # Byte-preserving current salvage retains its actual sticky taint and wire.
        for task in set(expected) - restored:
            row = dst.execute('SELECT wire,identity_state FROM kanban_sync_parent_envelopes WHERE task_id=?', (task,)).fetchone()
            assert row == (before[task], 'ambiguous')
    finally:
        dst.close()


def test_readable_current_parent_keeps_exact_original_tuple(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    path = home / 'current.db'
    conn = kbc.connect(path)
    with kbc.write_txn(conn):
        conn.execute('INSERT INTO kanban_sync_parent_envelopes '
                     '(task_id,wire,capture_protocol,original_op_id,original_created_at,identity_state) '
                     "VALUES ('t_current','{}','capture1','operation-current',123,'original')")
    before = conn.execute('SELECT * FROM kanban_sync_parent_envelopes').fetchall()
    before = list(map(tuple, before))
    conn.close()
    out = home / 'rebuilt.db'
    result = recovery.RecoveryResult(status='building', db_path=str(path))
    recovery._build(path, out, None, result)
    with sqlite3.connect(out) as dst:
        assert dst.execute('SELECT * FROM kanban_sync_parent_envelopes').fetchall() == before
    assert result.tables['kanban_sync_parent_envelopes'].recovered == 1
    assert result.tables['kanban_sync_parent_envelopes'].filled_from_backup == 0


@pytest.mark.parametrize('modern_target', [False, True])
def test_old_backup_parent_cannot_gain_originality_and_other_tables_still_fill(tmp_path, modern_target):
    backup = tmp_path / 'old.bak'
    with sqlite3.connect(backup) as bak:
        bak.execute('CREATE TABLE kanban_sync_parent_envelopes(task_id TEXT PRIMARY KEY,wire TEXT)')
        bak.execute("INSERT INTO kanban_sync_parent_envelopes VALUES ('t_old','{}')")
        bak.execute('CREATE TABLE business(id TEXT PRIMARY KEY,payload TEXT,identity_state TEXT)')
        bak.execute("INSERT INTO business VALUES ('known','preserve','original')")
        bak.execute('CREATE TABLE kanban_sync_outbox(op_id TEXT PRIMARY KEY,fields TEXT)')
        bak.execute("INSERT INTO kanban_sync_outbox VALUES ('old-operation','{}')")
    with sqlite3.connect(tmp_path / 'dst.db') as dst:
        suffix = ',identity_state TEXT' if modern_target else ''
        dst.execute(f'CREATE TABLE kanban_sync_parent_envelopes(task_id TEXT PRIMARY KEY,wire TEXT{suffix})')
        dst.execute('CREATE TABLE business(id TEXT PRIMARY KEY,payload TEXT,identity_state TEXT)')
        dst.execute('CREATE TABLE kanban_sync_outbox(op_id TEXT PRIMARY KEY,fields TEXT)')
        tables = [(r[0], r[1]) for r in dst.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")]
        listed = {
            'kanban_sync_parent_envelopes': {'key': (['task_id'], {1: ('t_old',)})},
            'business': {'key': (['id'], {1: ('known',)})},
            'kanban_sync_outbox': {'key': (['op_id'], {1: ('old-operation',)})},
        }
        result = recovery.RecoveryResult(status='building', db_path='fixture')
        recovery._fill_from_backup(dst, backup, tables, listed, result)
        parent = dst.execute('SELECT * FROM kanban_sync_parent_envelopes').fetchone()
        assert parent == (('t_old', '{}', 'ambiguous') if modern_target else ('t_old', '{}'))
        assert dst.execute('SELECT * FROM business').fetchone() == ('known', 'preserve', 'original')
        assert dst.execute('SELECT * FROM kanban_sync_outbox').fetchall() == []
        assert result.tables['kanban_sync_parent_envelopes'].filled_from_backup == 1
