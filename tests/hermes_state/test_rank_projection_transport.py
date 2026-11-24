"""Bound SQLite/Python page handoffs without changing SQLite value semantics."""
import math
import random
import sqlite3
import struct
import threading

import pytest

import hermes_state_rank_memo as memo


class RecordingConnection:
    def __init__(self, conn):
        self.conn = conn
        self.returned = 0
        self.statements = 0

    def execute(self, sql, params=()):
        self.statements += 1
        cursor = self.conn.execute(sql, params)
        owner = self

        class Cursor:
            description = cursor.description

            def fetchall(self):
                rows = cursor.fetchall()
                owner.returned += len(rows)
                return rows

            def fetchone(self):
                row = cursor.fetchone()
                owner.returned += row is not None
                return row

        return Cursor()


@pytest.mark.parametrize('count', [1, 200, 2000])
def test_project_has_constant_python_row_handoffs(count):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY, started REAL)')
    conn.executemany('INSERT INTO sessions VALUES (?, ?)', [(str(i), 1700000000.125) for i in range(count)])
    measured = RecordingConnection(conn)
    try:
        steps = [0]
        conn.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 100) or 0, 100)
        rows = memo.project(measured, memo.RankMemo(threading.Lock()),
                            [(str(i), None, None, i) for i in range(count)],
                            'SELECT s.id, s.started, ', '', 'unused', 's.started', 0)
        assert [r['id'] for r in rows] == [str(i) for i in range(count)]
        assert measured.statements <= 2
        assert steps[0] <= 150 * count + 1000, steps
        assert measured.returned <= 1, 'page must cross SQLite/Python as one packed row'
    finally:
        conn.close()


@pytest.mark.parametrize('reader', ['read_page', 'read_chunks'])
def test_packed_page_preserves_values_and_column_names(reader):
    import hermes_state_page_pack
    read_page = getattr(hermes_state_page_pack, reader)
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE cells(value)')
    values = [None, 0, 2**63 - 1, -(2**63), 'a\x00b\n"é', b'\x00\xff', 1.0000000000000002,
              1700000000.1234567, -0.0000000000000001, 5e-324, float('inf'), -float('inf')]
    randoms = random.Random(42)
    for _ in range(1000):
        value = struct.unpack('!d', randoms.getrandbits(64).to_bytes(8, 'big'))[0]
        if math.isfinite(value):
            values.append(value)
    conn.executemany('INSERT INTO cells VALUES (?)', [(v,) for v in values])
    try:
        rows = read_page(conn, 'SELECT value AS "a""b" FROM cells ORDER BY rowid', ())
        actual = [r['a"b'] for r in rows]
        assert actual == values
        assert [type(v) for v in actual] == [type(v) for v in values]
        assert math.isinf(actual[11])
        assert read_page(conn, 'SELECT value FROM cells WHERE 0', ()) == []
    finally:
        conn.close()
