"""Preserve locally frozen stop state while consuming ordinary Board feed pages."""
from hermes_cli.kanban_stop_ended import KIND


def is_replica(conn) -> bool:
    """Whether ``conn`` is a board's pulled copy (``sync-shadow/<board>.db``), never a gateway's live file."""
    from pathlib import Path
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.kanban_db_sync import SHADOW_DIR
    path = _main_db_file(conn)
    return bool(path) and Path(path).parent.name == SHADOW_DIR


def frozen_change(conn, change) -> bool:
    """Ignore lifecycle rewrites of an already stopped local card, including old feed echoes.

    Only the live file freezes. A replica mirrors the Board, which holds the card's later archive, comments and
    events; freezing it left paintbot-tools M5 ``scheduled`` for 40 h after its home archived it
    (t_01a21a1955976537).

    The serving Board prevents future foreign writes after its marker arrives. Previously
    accepted rows can still be in flight, so local fences must not turn their delivery into
    a board-wide page rollback. Keep the frozen bytes; the caller still advances its cursor.
    This is not authority to install a stop from an arbitrary remote event.
    """
    if is_replica(conn):
        return False
    table = change.table
    if table not in {'tasks', 'task_runs', 'task_events', 'task_links'}:
        return False
    ids = {change.uid} if table == 'tasks' else set()
    fields = change.fields or {}
    ids.update(fields.get(k) for k in ('task_id', 'parent_id', 'child_id') if fields.get(k))
    if table in {'task_runs', 'task_events'}:
        row = conn.execute(f'SELECT task_id FROM {table} WHERE uid=?', (change.uid,)).fetchone()
        if row:
            ids.add(row[0])
    if table == 'task_links':
        # Link deletes carry only the composite wire UID, with no fields.
        ids.update(change.uid.split('>', 1))
    return any(conn.execute('SELECT 1 FROM task_events WHERE task_id=? AND kind=? LIMIT 1',
                            (task_id, KIND)).fetchone() for task_id in ids)
