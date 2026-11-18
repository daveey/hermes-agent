"""Local, disposable certificates for Python-decoded empty creator events. [daveey]"""
from __future__ import annotations

import re
import sqlite3

_TABLE = 'kanban_creator_empty_events'
_TABLE_SQL = f'CREATE TABLE {_TABLE} (event_id INTEGER PRIMARY KEY)'
_TRIGGERS = {
    'creator_empty_insert': f'CREATE TRIGGER creator_empty_insert AFTER INSERT ON task_events '
                           f'BEGIN DELETE FROM {_TABLE} WHERE event_id=NEW.id; END',
    'creator_empty_update': f'CREATE TRIGGER creator_empty_update AFTER UPDATE ON task_events '
                           f'BEGIN DELETE FROM {_TABLE} WHERE event_id IN (OLD.id,NEW.id); END',
    'creator_empty_delete': f'CREATE TRIGGER creator_empty_delete AFTER DELETE ON task_events '
                           f'BEGIN DELETE FROM {_TABLE} WHERE event_id=OLD.id; END',
}
_EXPECTED = {('table', _TABLE): _TABLE_SQL,
             **{('trigger', name): sql for name, sql in _TRIGGERS.items()}}
_NAMES = (_TABLE, *_TRIGGERS)
_ROWS = ("SELECT t.id, t.session_id, e.payload, e.id AS creator_event_id "
         "FROM tasks t JOIN task_events e ON e.task_id=t.id AND e.kind='created' "
         "WHERE NOT EXISTS (SELECT 1 FROM task_events done WHERE done.task_id=t.id "
         "AND done.kind='creator_chats_resolved')")


def _schema(conn) -> dict:
    return {(kind, name): sql for kind, name, sql in conn.execute(
        'SELECT type, name, sql FROM sqlite_master WHERE name IN ('
        + ','.join('?' for _ in _NAMES) + ')', _NAMES)}


def _compatible(schema: dict) -> bool:
    """Only our table layout and trigger-typed names may be repaired."""
    return all(key in _EXPECTED and (key != ('table', _TABLE) or sql == _TABLE_SQL)
               for key, sql in schema.items())


def ensure(conn: sqlite3.Connection) -> bool:
    """Reinstall atomically after a capture gap; never trust pre-gap certificates.

    Intact installations perform no writes. Initial DDL deliberately invalidates
    schema-keyed repair/projection caches once. Unknown table layouts fall back.
    """
    from hermes_cli.kanban_db_connect import write_txn

    try:
        schema = _schema(conn)
        if schema == _EXPECTED:
            return True
        if not _compatible(schema):
            return False  # unknown schema must not acquire a writer every pass
        with write_txn(conn):
            schema = _schema(conn)
            if schema == _EXPECTED:
                return True
            if not _compatible(schema):
                return False
            if ('table', _TABLE) not in schema:
                conn.execute(_TABLE_SQL)
            for name, sql in _TRIGGERS.items():
                conn.execute(f'DROP TRIGGER IF EXISTS {name}')
                conn.execute(sql)
            conn.execute(f'DELETE FROM {_TABLE}')
        return True
    except sqlite3.Error:
        return False


_PRE_PATCH_ROWS = ("SELECT t.id, t.session_id, e.payload FROM tasks t JOIN task_events e ON e.task_id=t.id "
                   "AND e.kind='created' WHERE NOT EXISTS (SELECT 1 FROM task_events done WHERE done.task_id=t.id "
                   "AND done.kind='creator_chats_resolved')")
# LIMIT keeps SQLite from flattening or pushing the marker probe into the literal; the inner
# query then runs as a co-routine whose yield order is the literal's own driver order.
_ENVELOPE = (f'SELECT * FROM ({_ROWS} LIMIT -1 OFFSET 0) AS c WHERE NOT EXISTS '
             f'(SELECT 1 FROM {_TABLE} m WHERE m.event_id=c.creator_event_id)')
_SERIOUS = frozenset({7, 10, 11, 13, 26})  # NOMEM, IOERR, CORRUPT, FULL, NOTADB
_PROBE = 'SEARCH m USING INTEGER PRIMARY KEY (rowid=?)'
_SUBQ = re.compile(r'^(CORRELATED SCALAR SUBQUERY) \d+$')


def _serious(exc: sqlite3.Error) -> bool:
    return (getattr(exc, 'sqlite_errorcode', 0) & 0xff) in _SERIOUS


def _plan(conn, sql: str) -> list | None:
    """EXPLAIN QUERY PLAN as (depth, detail); ids and parent numbers are connection noise."""
    depth, plan = {0: -1}, []
    for row in conn.execute('EXPLAIN QUERY PLAN ' + sql):
        if len(row) != 4 or row[1] not in depth:
            return None  # Unknown EQP shape: use the literal and Python filter.
        node, parent, _unused, detail = row
        depth[node] = depth[parent] + 1
        plan.append((depth[node], detail))
    return plan


def _envelope_plan_ok(conn) -> bool:
    """Per call: the envelope must be the literal's own plan as a co-routine, then a PK probe."""
    literal, envelope = _plan(conn, _ROWS), _plan(conn, _ENVELOPE)
    if not literal or literal != _plan(conn, _PRE_PATCH_ROWS):
        return False
    n = len(literal)
    if envelope is None or envelope[:1] != [(0, 'CO-ROUTINE c')] or envelope[1:n + 1] != [(d + 1, x) for d, x in literal]:
        return False
    tail = [(d, _SUBQ.sub(r'\1', x)) for d, x in envelope[n + 1:]]
    return tail == [(0, 'SCAN c'), (0, 'CORRELATED SCALAR SUBQUERY'), (1, _PROBE)]


def candidates(conn: sqlite3.Connection) -> list:
    """Return the literal query's rows minus certified empties from one deferred snapshot.

    Adding an anti-join to the SELECT can change SQLite's driver after ANALYZE, and backfill
    observes earlier inheritance in that order. The marker probe therefore wraps the unchanged
    literal behind a LIMIT barrier; a per-call plan guard falls back to Python filtering when
    this SQLite would plan it differently. A caller-owned transaction uses the original
    unfiltered path without DDL. Serious SQLite errors propagate; harmless refusals fall back.
    """
    if conn.in_transaction or not ensure(conn):
        return conn.execute(_ROWS).fetchall()
    conn.execute('BEGIN')
    try:
        try:
            if _schema(conn) != _EXPECTED:
                return conn.execute(_ROWS).fetchall()
        except sqlite3.Error as exc:
            if _serious(exc):
                raise
            return conn.execute(_ROWS).fetchall()
        try:
            if _envelope_plan_ok(conn):
                return conn.execute(_ENVELOPE).fetchall()
        except sqlite3.Error as exc:
            if _serious(exc):
                raise
            return conn.execute(_ROWS).fetchall()
        rows = conn.execute(_ROWS).fetchall()
        try:
            marked = {r[0] for r in conn.execute(f'SELECT event_id FROM {_TABLE}')}
        except sqlite3.Error as exc:
            if _serious(exc):
                raise
            return rows
        return [row for row in rows if row['creator_event_id'] not in marked]
    finally:
        conn.rollback()  # release only our read snapshot before provenance/writes


def write_coverage(conn: sqlite3.Connection) -> bool:
    """Check once after the pass acquires its writer; peers cannot change DDL now."""
    if not conn.in_transaction:
        return False
    try:
        return _schema(conn) == _EXPECTED
    except sqlite3.Error as exc:
        if not conn.in_transaction or (getattr(exc, 'sqlite_errorcode', 0) & 0xff) in (11, 26):
            raise
        return False


def remember_empty(conn: sqlite3.Connection, row) -> None:
    """Called only after successful falsy Python decode, inside backfill's txn.

    Bind the original storage value, including BLOB/NULL. A concurrent rewrite
    between selection and the write lock must not certify the changed payload.
    Failure to retain an optimization never replaces the original repair work.
    """
    try:
        conn.execute(f'INSERT OR IGNORE INTO {_TABLE}(event_id) '
                     'SELECT id FROM task_events WHERE id=? AND task_id=? '
                     "AND kind='created' AND payload IS ?",
                     (row['creator_event_id'], row['id'], row['payload']))
    except sqlite3.Error as exc:
        if not conn.in_transaction or (getattr(exc, 'sqlite_errorcode', 0) & 0xff) in (11, 26):
            raise  # SQLite may have rolled back the outer txn; never continue in autocommit.
        # Harmless optimization refusal (e.g. SQLITE_AUTH) leaves repair atomic.
