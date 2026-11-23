"""Bounded scalar inputs covered by the native task/session clocks. [daveey]

These are content inputs, NOT full board/annotation payloads. Related task tables
and operational fields need separate declared hydration/revision coverage; do not
silently use these rows as complete TaskFact/SessionFact schemas. No transcript
bodies, filesystem discovery, external calls or connection lifetime live here.
"""
from hermes_cli.kanban_activity_revision import BOOKKEEPING_COLUMNS


def _rows(conn, ids, table, excluded):
    if not conn.in_transaction:
        raise ValueError('content hydration requires capture transaction')
    if not ids:
        return ()
    if len(ids) > 500 or any(type(identity) is not str for identity in ids):
        raise ValueError('invalid hydration id batch')
    # Table is chosen exclusively by the two fixed entrypoints below; quote
    # schema-derived column identifiers rather than trusting future migrations.
    columns = [row[1] for row in conn.execute(f'PRAGMA table_info({table})') if row[1] not in excluded]
    if 'id' not in columns:
        raise ValueError('content table lacks identity')
    names = ','.join('"' + name.replace('"', '""') + '"' for name in columns)
    placeholders = ','.join('?' for _ in ids)
    result = conn.execute(f'SELECT {names} FROM {table} WHERE id IN ({placeholders}) ORDER BY id', ids)
    return (dict(zip(columns, row)) for row in result)


def task_rows(conn, ids):
    """All task scalar columns except canonical cursor-excluded bookkeeping."""
    return _rows(conn, ids, 'tasks', BOOKKEEPING_COLUMNS['tasks'])


def session_rows(conn, ids):
    """Listing + content clocks jointly cover every session scalar column."""
    return _rows(conn, ids, 'sessions', frozenset())
