"""Fenced raw snapshots for the annotation-only semantic gate."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_replica_rows as rr


def store(path):
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''CREATE TABLE replica(gw TEXT, coll TEXT, key TEXT, data TEXT,
        updated_at REAL, version INTEGER, seq INTEGER);
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
        INSERT INTO meta VALUES('gateway', 'origin');
        INSERT INTO meta VALUES('last_ok', '100');''')
    conn.execute("INSERT INTO replica VALUES('owner','tasks','default/main/t',?,100,1,1)",
                 (json.dumps({'status': 'todo'}),))
    conn.commit()
    return conn


def take(reader, conn, path):
    conn.execute('BEGIN')
    try:
        return reader.read(conn, path)
    finally:
        conn.rollback()


def factory():
    from hermes_cli.kanban_annotation_reader import ReplicaReader
    return ReplicaReader()


def test_requires_snapshot(tmp_path):
    path = tmp_path / 'hub.db'
    with store(path) as conn:
        with pytest.raises(ValueError, match='transaction'):
            factory().read(conn, path)


def test_unchanged_rows_reused_but_gateway_read_each_time(tmp_path):
    path = tmp_path / 'hub.db'
    writer = store(path)
    conn = sqlite3.connect(path)
    reader = factory()
    try:
        first = take(reader, conn, path)
        second = take(reader, conn, path)
        assert second.rows is first.rows
        assert second.gateway == 'origin'
        writer.execute("INSERT INTO replica VALUES('owner','gateway','info','{}',100,1,2)")
        writer.commit()
        third = take(reader, conn, path)
        assert third.info_rows == (('owner', '{}'),)
        assert third.last_ok == 100
    finally:
        conn.close()
        writer.close()


def test_unrelated_commit_is_zero_row_delta(tmp_path):
    path = tmp_path / 'hub.db'
    writer = store(path)
    conn = sqlite3.connect(path)
    reader = factory()
    try:
        first = take(reader, conn, path)
        writer.execute("INSERT INTO replica VALUES('owner','sessions','s','{}',100,1,5)")
        writer.commit()
        second = take(reader, conn, path)
        assert second.rows is first.rows  # task seq probe unmoved: no probe scan, no delta, no full re-read
    finally:
        conn.close()
        writer.close()


def test_seq_less_rewrite_is_outside_writer_protocol(tmp_path):
    """hermes_hub stamps every task write with a new seq; a raw seq-less rewrite (no such writer exists) is not
    seen by the narrow reader. Display-only: the holder's 20 s renewal and the exact strict paths bound it."""
    path = tmp_path / 'hub.db'
    writer = store(path)
    conn = sqlite3.connect(path)
    reader = factory()
    try:
        first = take(reader, conn, path)
        writer.execute("UPDATE replica SET data=replace(data,'todo','finished')")  # seq kept
        writer.commit()
        assert take(reader, conn, path).rows is first.rows
        writer.execute("UPDATE replica SET seq=7, data=replace(data,'finished','done')")  # the writer's protocol
        writer.commit()
        assert json.loads(take(reader, conn, path).rows.rows()[0][2])['status'] == 'done'
    finally:
        conn.close()
        writer.close()


def test_new_connection_never_compares_data_versions(tmp_path):
    path = tmp_path / 'hub.db'
    writer = store(path)
    reader = factory()
    conn = sqlite3.connect(path)
    first = take(reader, conn, path)
    conn.close()
    writer.execute("UPDATE replica SET data=replace(data,'todo','done')")
    writer.commit()
    conn = sqlite3.connect(path)
    try:
        second = take(reader, conn, path)
        assert second.rows is not first.rows
        assert 'done' in second.rows.rows()[0][2]
    finally:
        conn.close()
        writer.close()


def test_null_sequence_fails_closed_and_recovers(tmp_path):
    path = tmp_path / 'hub.db'
    writer = store(path)
    conn = sqlite3.connect(path)
    reader = factory()
    try:
        take(reader, conn, path)
        writer.execute('UPDATE replica SET seq=NULL')
        writer.commit()
        with pytest.raises(ValueError, match='sequence'):
            take(reader, conn, path)
        writer.execute("UPDATE replica SET seq=1, data=replace(data,'todo','done')")
        writer.commit()
        assert 'done' in take(reader, conn, path).rows.rows()[0][2]
    finally:
        conn.close()
        writer.close()


def test_sequence_delta_and_deletion(tmp_path):
    path = tmp_path / 'hub.db'
    writer = store(path)
    conn = sqlite3.connect(path)
    reader = factory()
    try:
        first = take(reader, conn, path)
        writer.execute("UPDATE replica SET seq=2, version=2, data=replace(data,'todo','done')")
        writer.commit()
        second = take(reader, conn, path)
        assert second.rows.base_gen == first.rows.gen
        assert second.rows.changed == frozenset({('owner', 'default/main/t')})
        assert 'done' in second.rows.rows()[0][2]
        writer.execute('DELETE FROM replica')
        writer.commit()
        third = take(reader, conn, path)
        assert third.rows.rows() == []
        assert third.rows.removed == frozenset({('owner', 'default/main/t')})
    finally:
        conn.close()
        writer.close()


def test_error_clears_successful_view(tmp_path):
    path = tmp_path / 'hub.db'
    writer = store(path)
    reader = factory()
    try:
        take(reader, writer, path)
        writer.execute("UPDATE meta SET v='nan' WHERE k='last_ok'")
        writer.commit()
        with pytest.raises(ValueError, match='nonfinite'):
            take(reader, writer, path)
        assert reader._rows is None
        writer.execute("UPDATE meta SET v='101' WHERE k='last_ok'")
        writer.commit()
        assert take(reader, writer, path).last_ok == 101
    finally:
        writer.close()


def test_same_inode_restore_with_different_totals_reads_in_full(tmp_path):
    path = tmp_path / 'hub.db'
    writer = store(path)
    reader = factory()
    conn = sqlite3.connect(path)
    backup = sqlite3.connect(tmp_path / 'backup.db')
    try:
        writer.backup(backup)
        backup.execute("UPDATE replica SET data=replace(data,'todo','restored')")
        backup.commit()
        first = take(reader, conn, path)
        inode = path.stat().st_ino
        backup.backup(writer)
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        assert path.stat().st_ino == inode
        second = take(reader, conn, path)
        assert second.rows.gen != first.rows.gen
        assert 'restored' in second.rows.rows()[0][2]
    finally:
        backup.close()
        conn.close()
        writer.close()


def test_checkpoint_does_not_force_full_read(tmp_path):
    path = tmp_path / 'hub.db'
    writer = store(path)
    conn = sqlite3.connect(path)
    reader = factory()
    try:
        first = take(reader, conn, path)
        writer.execute("UPDATE replica SET seq=2, version=2, data=replace(data,'todo','done')")
        writer.commit()
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        second = take(reader, conn, path)
        assert second.rows.base_gen == first.rows.gen
        assert 'done' in second.rows.rows()[0][2]
    finally:
        conn.close()
        writer.close()


def test_file_changes_during_read_not_published(tmp_path, monkeypatch):
    from hermes_cli import kanban_annotation_reader as ar
    path = tmp_path / 'hub.db'
    conn = store(path)
    reader = factory()
    real = ar._file_fence
    calls = []

    def fence(p):
        calls.append(p)
        result = real(p)
        return result if len(calls) == 1 else (*result, 'moved')

    monkeypatch.setattr(ar, '_file_fence', fence)
    try:
        with pytest.raises(ValueError, match='file changed'):
            take(reader, conn, path)
        assert reader._rows is None
    finally:
        conn.close()


def test_never_uses_strict_global_rows_cache(tmp_path, monkeypatch):
    path = tmp_path / 'hub.db'
    conn = store(path)
    monkeypatch.setattr(rr, 'read', lambda *a: pytest.fail('shared strict cache'))
    try:
        assert take(factory(), conn, path).rows.rows()
    finally:
        conn.close()
