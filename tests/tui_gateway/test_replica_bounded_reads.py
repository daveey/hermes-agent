"""Large replicas must not cross the SQLite/Python boundary once per task."""
import json
import sqlite3

import pytest

from tui_gateway import kanban_remote_notices as remote


class CountingCursor(sqlite3.Cursor):
    def fetchall(self):
        rows = super().fetchall()
        self.connection.fetched += len(rows)
        return rows

    def fetchone(self):
        row = super().fetchone()
        self.connection.fetched += row is not None
        return row

    def __next__(self):
        row = super().__next__()
        self.connection.fetched += 1
        return row


class CountingConnection(sqlite3.Connection):
    fetched = 0

    def execute(self, sql, parameters=()):
        return self.cursor(CountingCursor).execute(sql, parameters)


@pytest.mark.parametrize('modern', [False, True])
@pytest.mark.parametrize('me', [None, 'local'])
def test_task_rows_preserve_raw_values_with_bounded_fetches(modern, me):
    conn = sqlite3.connect(':memory:', factory=CountingConnection)
    try:
        extra = ', seq INTEGER, version INTEGER' if modern else ''
        conn.execute('CREATE TABLE replica (gw TEXT, key TEXT, data TEXT, updated_at INTEGER, coll TEXT' + extra + ')')
        rows = [('local' if i % 3 == 0 else 'remote', f'default/default/t_{i}',
                 '{bad json' if i == 1 else json.dumps({'id': f't_{i}', 'body': 'quote " slash \\ café\n'}),
                 i, 'tasks') + ((i + 1, i) if modern else ()) for i in range(12000)]
        conn.executemany('INSERT INTO replica VALUES (' + ','.join('?' for _ in rows[0]) + ')', rows)
        seq = 'seq' if modern else '0'
        sql = f"SELECT gw, key, {seq}, data FROM replica WHERE coll = 'tasks'"
        expected = conn.execute(sql + ' AND gw != ?', (me,)).fetchall() if me else conn.execute(sql).fetchall()
        conn.fetched = 0
        actual = remote._task_rows(conn, me)
        assert actual == expected
        assert conn.fetched <= 12, f'{conn.fetched} result rows crossed the SQLite/Python boundary'
    finally:
        conn.close()


@pytest.mark.parametrize('modern', [False, True])
def test_remote_links_miss_preserves_rows_with_bounded_fetches(tmp_path, monkeypatch, modern):
    from hermes_cli import kanban_session_activity as activity

    path = tmp_path / 'hub.db'
    conn = sqlite3.connect(path, factory=CountingConnection)
    extra = ', seq INTEGER, version INTEGER' if modern else ''
    conn.execute('CREATE TABLE replica (gw TEXT, key TEXT, data TEXT, updated_at INTEGER, coll TEXT' + extra + ')')
    rows = [('remote', f'default/default/t_{i}', '{bad' if i == 1 else json.dumps({'id': f't_{i}'}),
             i, 'tasks') + ((i + 1, i) if modern else ()) for i in range(12000)]
    conn.executemany('INSERT INTO replica VALUES (' + ','.join('?' for _ in rows[0]) + ')', rows)
    conn.commit()
    version = 'version' if modern else 'NULL'
    expected = conn.execute(f"SELECT gw, key, data, updated_at, {version} FROM replica WHERE coll = 'tasks'").fetchall()
    conn.fetched = 0
    monkeypatch.setattr(activity, '_open_ro', lambda path: conn)
    monkeypatch.setattr(activity, '_self_gateway', lambda conn: 'local')
    monkeypatch.setattr(activity, '_meta_float', lambda conn, key: None)
    monkeypatch.setattr(activity, '_remote_read_memo', None)
    monkeypatch.setattr(activity, '_project_replica', lambda path, now, me, rows, *args, **kwargs: rows)
    assert activity._read_remote_links(path) == expected
    assert conn.fetched <= 12


def test_warm_observation_identity_scan_is_bounded(tmp_path, monkeypatch):
    from tui_gateway import kanban_remote_observations as observations

    conn = sqlite3.connect(':memory:', factory=CountingConnection)
    replica = tmp_path / 'replica.db'
    rows = [('remote', f'default/default/t_{i}', i + 1, '{}', 'tasks') for i in range(12000)]
    monkeypatch.setattr(observations, '_decoded', {
        (str(replica), owner, key): (seq, {'status': 'done'})
        for owner, key, seq, _, _ in rows
    })
    try:
        conn.execute('CREATE TABLE replica (gw TEXT, key TEXT, seq INTEGER, data TEXT, coll TEXT)')
        conn.executemany('INSERT INTO replica VALUES (?,?,?,?,?)', rows)
        conn.execute("DELETE FROM replica WHERE key='default/default/t_1'")
        conn.execute("UPDATE replica SET seq=13000, data='{bad' WHERE key='default/default/t_2'")
        conn.fetched = 0
        result = observations._changed_rows_cards(conn, replica, 'local')
        assert result == [(owner, key, {'status': 'done'}) for owner, key, seq, _, _ in rows if seq not in (2, 3)]
        assert (str(replica), 'remote', 'default/default/t_1') not in observations._decoded
        assert conn.fetched <= 3
    finally:
        conn.close()


def test_warm_notice_identity_scan_is_bounded_and_detects_deletes():
    from tui_gateway.remote_index_projection import _read_rows

    conn = sqlite3.connect(':memory:', factory=CountingConnection)
    try:
        conn.execute('CREATE TABLE replica (gw TEXT, key TEXT, seq INTEGER, data TEXT, coll TEXT)')
        rows = [('remote', f'default/default/t_{i}', i + 1, '{}', 'tasks') for i in range(12000)]
        conn.executemany('INSERT INTO replica VALUES (?,?,?,?,?)', rows)
        previous = {(owner, key): (seq,) for owner, key, seq, _, _ in rows}
        conn.execute("DELETE FROM replica WHERE key='default/default/t_1'")
        conn.execute("UPDATE replica SET seq=13000, data='new' WHERE key='default/default/t_2'")
        conn.commit()
        expected = [(owner, key, 13000 if seq == 3 else seq, 'new' if seq == 3 else None)
                    for owner, key, seq, _, _ in rows if seq != 2]
        conn.fetched = 0
        assert _read_rows(conn, 'local', previous) == expected
        assert conn.fetched <= 3
    finally:
        conn.close()
