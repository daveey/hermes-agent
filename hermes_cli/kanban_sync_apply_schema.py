"""Page-local ordered sync schema, prepared before acquiring the board writer."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json


@dataclass
class Schema:
    conn: object
    version: int
    columns: dict[str, tuple[str, ...]]


_SCHEMA: ContextVar[Schema | None] = ContextVar('kanban_sync_apply_schema', default=None)


def _prepare(conn, tables):
    # Preserve unqualified PRAGMA resolution for unusual attached/temp layouts.
    if any(row[1] not in {'main', 'temp'} for row in conn.execute('PRAGMA database_list')):
        return None
    placeholders = ','.join('?' for _ in tables)
    if conn.execute(f'SELECT 1 FROM sqlite_temp_master WHERE name IN ({placeholders}) LIMIT 1', tables).fetchone():
        return None
    # One SQLite snapshot/result row: no Python/GIL handoff for each column.
    probes = ', '.join('(SELECT json_group_array(name) FROM (SELECT name FROM pragma_table_info(?) ORDER BY cid))' for _ in tables)
    row = conn.execute('SELECT (SELECT schema_version FROM pragma_schema_version), ' + probes, tables).fetchone()
    return Schema(conn, row[0], {table: tuple(json.loads(cell)) for table, cell in zip(tables, tuple(row)[1:])})


@contextmanager
def page_writer(conn, tables, *, cache):
    """Keep the existing atomic page; cache only immutable schema, never outbox/row state.

    A rare concurrent migration refreshes the fixed-size schema snapshot under the
    acquired writer. Arbitrary schema-changing keep callbacks use the old path.
    This shortens a writer transaction; it is not a page wall-clock deadline.
    """
    from hermes_cli.kanban_db_connect import write_txn
    prepared = _prepare(conn, tables) if cache else None
    with write_txn(conn):
        if prepared is not None and conn.execute('PRAGMA schema_version').fetchone()[0] != prepared.version:
            prepared = _prepare(conn, tables)
        token = _SCHEMA.set(prepared)
        try:
            yield
        finally:
            _SCHEMA.reset(token)


def table_columns(conn, table):
    schema = _SCHEMA.get()
    if schema is not None and schema.conn is conn and table in schema.columns:
        return list(schema.columns[table])
    row = conn.execute('SELECT json_group_array(name) FROM (SELECT name FROM pragma_table_info(?) ORDER BY cid)',
                       (table,)).fetchone()
    return json.loads(row[0])


def tables_columns(conn, *tables):
    """Containment's combined fallback stays one aggregate result outside page scope."""
    schema = _SCHEMA.get()
    if schema is not None and schema.conn is conn and all(table in schema.columns for table in tables):
        return tuple(frozenset(schema.columns[table]) for table in tables)
    from hermes_cli.kanban_table_columns import tables_columns as uncached
    return uncached(conn, *tables)
