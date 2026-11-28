"""Bound sync diff enumeration without changing its exact Python partitions."""
import random
import sqlite3

import pytest

from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import sync_diff_predicate_helpers as helpers

inventory = helpers.inventory


@pytest.mark.parametrize('table', helpers.TABLES)
@pytest.mark.parametrize('gateway', [None, 'mine', 'other'])
def test_exact_partition_and_legacy_membership(inventory, table, gateway):
    conn, ids = inventory
    selections = [set(), set(ids), {'A'}, {'a'}, {'absent'}, {'x\x00z'},
                  {None, 1, b'x', 'A'}, {'\ud800', 'A'}]
    rng = random.Random(441)
    selections += [set(rng.sample(ids, rng.randrange(len(ids)))) for _ in range(15)]
    before = conn.total_changes
    for selected in selections:
        assert client._child_uids(conn, table, selected, gateway) == helpers.original(conn, table, selected, gateway)
    assert conn.total_changes == before
    assert not conn.in_transaction


@pytest.mark.parametrize('size', [0, 1, 399, 400, 401, 800, 801, 1200])
def test_single_statement_and_small_bind_limit(inventory, size):
    conn, _ = inventory
    old = conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 16)
    trace = []
    conn.set_trace_callback(trace.append)
    try:
        selected = {'A'} | {f'absent-{i}' for i in range(size)}
        assert client._child_uids(conn, 'task_comments', selected) == {('A', 'u-0')}
        assert len(trace) == 1
        assert trace[0].startswith('SELECT ')
    finally:
        conn.set_trace_callback(None)
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old)


@pytest.mark.parametrize('table', helpers.TABLES)
def test_selected_invalid_utf8_still_fails_closed(inventory, table):
    conn, _ = inventory
    if table == 'task_links':
        conn.execute("INSERT INTO task_links VALUES (CAST(X'80' AS TEXT),'bad')")
    else:
        conn.execute(f"INSERT INTO {table}(task_id,uid) VALUES ('bad',CAST(X'80' AS TEXT))")
    for read in (helpers.original, client._child_uids):
        with pytest.raises(sqlite3.OperationalError):
            read(conn, table, {'bad'})


@pytest.mark.parametrize('table', helpers.TABLES)
@pytest.mark.parametrize('selection', [set(), {'A'}])
def test_excluded_malformed_projection_remains_fail_closed(inventory, table, selection):
    conn, _ = inventory
    if table == 'task_links':
        conn.execute("INSERT INTO task_links VALUES (CAST(X'80' AS TEXT),'excluded')")
    else:
        conn.execute(f"INSERT INTO {table}(task_id,uid) VALUES ('excluded',CAST(X'80' AS TEXT))")
    with pytest.raises(sqlite3.OperationalError):
        helpers.original(conn, table, selection, 'mine')
    with pytest.raises(sqlite3.OperationalError):
        client._child_uids(conn, table, selection, 'mine')


def test_single_snapshot_sees_no_half_commit(tmp_path):
    path = tmp_path / 'rows.db'
    reader = sqlite3.connect(path)
    writer = sqlite3.connect(path)
    try:
        reader.execute('PRAGMA journal_mode=WAL')
        reader.execute('CREATE TABLE task_comments(task_id TEXT,uid TEXT)')
        reader.executemany('INSERT INTO task_comments VALUES (?,?)', [('a', 'old-a'), ('b', 'old-b')])
        reader.commit()
        fired = False

        def interleave(cursor, row):
            nonlocal fired
            if not fired:
                fired = True
                writer.execute("UPDATE task_comments SET uid='new-b' WHERE task_id='b'")
                writer.commit()
            return row

        reader.row_factory = interleave
        before = reader.total_changes
        assert client._child_uids(reader, 'task_comments', {'a', 'b'}) == {('a', 'old-a'), ('b', 'old-b')}
        assert fired and reader.total_changes == before
        assert client._child_uids(reader, 'task_comments', {'a', 'b'}) == {('a', 'old-a'), ('b', 'new-b')}
        assert not reader.in_transaction
    finally:
        reader.close()
        writer.close()


class CapturingConnection(sqlite3.Connection):
    def execute(self, *args, **kwargs):
        cursor = super().execute(*args, **kwargs)
        self.last_cursor = cursor
        return cursor


def test_plain_row_cursor_avoids_wrappers_and_closes():
    conn = sqlite3.connect(':memory:', factory=CapturingConnection)
    try:
        conn.execute('CREATE TABLE task_comments(task_id TEXT, uid TEXT)')
        conn.execute("INSERT INTO task_comments VALUES ('mine','kept')")
        conn.commit()
        conn.row_factory = sqlite3.Row
        assert client._child_uids(conn, 'task_comments', {'mine'}) == {('mine', 'kept')}
        assert conn.row_factory is sqlite3.Row
        assert conn.last_cursor.row_factory is None
        with pytest.raises(sqlite3.ProgrammingError, match='closed cursor'):
            conn.last_cursor.fetchone()
    finally:
        conn.close()

