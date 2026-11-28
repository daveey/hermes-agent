"""Compact history seals keep byte and SQLite storage-class fidelity."""
import sqlite3

from plugins.kanban.dashboard import board_event_history as beh


def encoded(conn, columns):
    return [r[0] for r in conn.execute(
        f'SELECT CAST({beh.row_text(columns)} AS BLOB) FROM events ORDER BY id')]


def test_text_history_encoding_does_not_hex_expand_payloads():
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE events (id INTEGER PRIMARY KEY, payload)')
        payload = 'ordinary ASCII diagnostic payload ' * 100
        conn.execute('INSERT INTO events(payload) VALUES (?)', (payload,))
        result = encoded(conn, ('payload',))[0]
        assert len(result) < len(payload) + 20


def test_byte_exact_text_blob_types_and_record_boundaries():
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE events (id INTEGER PRIMARY KEY, payload)')
        values = [None, 1, 1.0, '1', b'1', '', b'', 'a\x00b', 'a',
                  '\n', '\\n', '\x1e', '\\u001e', '[null]', '{"b":"31"}',
                  'x,y|z', 2**62, 1e300, 1.000000000000001]
        conn.executemany('INSERT INTO events(payload) VALUES (?)', [(v,) for v in values])
        conn.execute("INSERT INTO events(payload) VALUES (CAST(x'80ff00' AS TEXT))")
        rows = encoded(conn, ('payload',))
        assert len(set(rows)) == len(values) + 1
        assert all(b'\n' not in row for row in rows)
        assert b'\x80\xff' in rows[-1], 'invalid UTF-8 remains bytes, not decoded or replaced'
