"""Transactional custody inputs for retained maintenance results. [daveey]

Independent of task/content revisions: an exact abort or pending clear can
change eligibility without touching any task. Unknown coverage disables reuse.
"""
from __future__ import annotations

TABLES = ('kanban_home_prepared1', 'kanban_home_finalized1',
          'kanban_home_aborted1', 'kanban_home_generations1')
_COUNTER = 'kanban_home_revision1'


def install(conn, tables):
    conn.execute(f'CREATE TABLE IF NOT EXISTS {_COUNTER} (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)')
    conn.execute(f'INSERT OR IGNORE INTO {_COUNTER} VALUES (1,0)')
    for table in tables:
        if table not in TABLES:
            raise ValueError('unsupported custody revision table')
        for action in ('INSERT', 'UPDATE', 'DELETE'):
            name = f'home_revision1_{table}_{action.lower()}'
            conn.execute(f'CREATE TRIGGER IF NOT EXISTS {name} AFTER {action} ON {table} '
                         f'BEGIN UPDATE {_COUNTER} SET revision=revision+1 WHERE id=1; END')


def read(conn):
    """O(1) counter, with bounded schema coverage proof; None means uncached."""
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?)", TABLES)}
    if not names:
        return 0
    for table in names:
        for action in ('insert', 'update', 'delete'):
            name = f'home_revision1_{table}_{action}'
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
            expected = (f'CREATE TRIGGER {name} AFTER {action.upper()} ON {table} '
                        f'BEGIN UPDATE {_COUNTER} SET revision=revision+1 WHERE id=1; END')
            if row is None or row[0] != expected:
                return None
    row = conn.execute(f'SELECT revision FROM {_COUNTER} WHERE id=1').fetchone()
    return int(row[0]) if row is not None else None
