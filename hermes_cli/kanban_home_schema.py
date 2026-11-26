"""Atomic lazy-schema coverage for dormant home preparation. [daveey]

Native outer write_txn owns persistent custody DDL as well as DML. Existing
per-table SQL triggers still reject ordinary statements, including raw clients.
For a newly created table, this boundary rejects protected rows before COMMIT
and installs durable triggers on empty/healthy-only tables in that same writer.

This is cooperative committed-state fencing, not hostile-SQL sandboxing. Native
writers must not end their own transaction, manipulate schema_version, remove
and recreate guards, or publish effects from uncommitted work. TEMP/attached
custody and arbitrary external DDL writers are outside this protocol.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar

from hermes_cli import kanban_home_abort as abort
from hermes_cli import kanban_home_finalized as finalized

_TABLE = 'kanban_home_prepared1'
_EXEMPT = {_TABLE, 'kanban_home_finalized1', 'kanban_home_aborted1',
           'containment_canonical_receipts'}
_RESTRICTIVE = {'containment_sync_quarantine', 'containment_sync_quarantine_tasks'}
_FIRST_INSTALL: ContextVar[tuple[sqlite3.Connection, list[tuple | None]] | None] = ContextVar(
    'home_schema_first_install', default=None)


@contextmanager
def first_install_scope(conn):
    """Keep speculative installation evidence only for this outer writer.

    A nested savepoint can roll back the first install. A later fresh install
    replaces that snapshot; ordinary repeated prepare calls cannot rebaseline
    already installed guards after intervening DDL.
    """
    snapshot: list[tuple | None] = [None]
    token = _FIRST_INSTALL.set((conn, snapshot))
    try:
        yield snapshot
    finally:
        _FIRST_INSTALL.reset(token)


def installed_first(conn, first):
    """Called immediately after the first guard installation, before user DDL."""
    scope = _FIRST_INSTALL.get()
    if first and scope is not None and scope[0] is conn:
        scope[1][0] = (conn.execute('PRAGMA schema_version').fetchone()[0],
                       _identities(conn, _tables(conn)))


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _installed(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)).fetchone())


def _active(conn):
    valid = finalized.predicate('p')
    return (f'SELECT p.task_id FROM {_TABLE} p WHERE NOT EXISTS('
            f'SELECT 1 FROM kanban_home_finalized1 f WHERE {valid}) AND NOT EXISTS('
            f'SELECT 1 FROM kanban_home_aborted1 a WHERE {abort.predicate("p")})')


def _tables(conn):
    tables = {}
    names = list(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    for (name,) in names:
        if name in _EXEMPT:
            continue
        columns = {r[1] for r in conn.execute(f'PRAGMA table_info({_quote(name)})')}
        if name in ('tasks', 'task_links') or 'task_id' in columns:
            tables[name] = columns
    return tables


def _covered(conn, table):
    actions = ('update', 'delete') if table in _RESTRICTIVE else ('insert', 'update', 'delete')
    return all(conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=? AND name=?",
                            (table, f'home_prepared1_{table}_{action}')).fetchone() for action in actions)


def _identities(conn, tables):
    # Index definitions matter: a new UNIQUE key changes REPLACE's deletion set.
    # Auto-index SQL is NULL, but table SQL carries its declared constraints.
    guards = {f'home_prepared1_{table}_{action}' for table in tables
              for action in ('insert', 'update', 'delete')}
    return {(r[0], r[1]): (r[2], r[3]) for r in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('table','index','trigger')")
        if r[2] in tables and (r[0] != 'trigger' or r[1] in guards)}


def _cover(conn):
    tables = _tables(conn)
    missing = [name for name in tables if not _covered(conn, name)]
    active = _active(conn)
    for name in missing:
        if name in _RESTRICTIVE:
            continue  # restrictive append is deliberately allowed; its immutable guard still installs
        columns = ('id',) if name == 'tasks' else ('parent_id', 'child_id') if name == 'task_links' else ('task_id',)
        condition = ' OR '.join(f'{_quote(col)} IN ({active})' for col in columns)
        if conn.execute(f'SELECT 1 FROM {_quote(name)} WHERE {condition} LIMIT 1').fetchone():
            raise sqlite3.IntegrityError('home transfer prepared: uncovered custody table')
    if missing:
        from hermes_cli.kanban_home_transfer import _install
        _install(conn)
    return tables


def begin(conn):
    """Called under the outer BEGIN reservation, before application statements."""
    if not _installed(conn):
        return None
    version = conn.execute('PRAGMA schema_version').fetchone()[0]
    cached = getattr(conn, '_home_schema_committed', None)
    if cached is not None and cached[0] == version:
        return cached
    tables = _cover(conn)
    return conn.execute('PRAGMA schema_version').fetchone()[0], _identities(conn, tables)


def finish(conn, before, first_install=None):
    """Fail closed before COMMIT; all failures use write_txn's ordinary rollback."""
    if not _installed(conn):
        if before is not None:
            raise sqlite3.IntegrityError('home transfer prepared: preparation schema removed')
        return
    if before is None and first_install is not None:
        before = first_install[0]
    version = conn.execute('PRAGMA schema_version').fetchone()[0]
    if before is not None and before[0] == version:
        return before
    tables = _tables(conn)
    if before is not None and conn.execute(_active(conn) + ' LIMIT 1').fetchone():
        current = _identities(conn, tables)
        if any(current.get(key) != value for key, value in before[1].items()):
            raise sqlite3.IntegrityError('home transfer prepared: guarded schema changed')
        old_tables = {value[0] for value in before[1].values()}
        if any(key not in before[1] and value[0] in old_tables for key, value in current.items()):
            raise sqlite3.IntegrityError('home transfer prepared: guarded identity changed')
    tables = _cover(conn)
    return conn.execute('PRAGMA schema_version').fetchone()[0], _identities(conn, tables)


def committed(conn, snapshot):
    """Cache only a successful COMMIT, never speculative/savepoint schema cookies."""
    if hasattr(conn, '__dict__'):
        conn._home_schema_committed = snapshot
