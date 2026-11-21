"""Hub-replica task-id resolution for ``kanban_session_activity`` (re-exported there).

Facade names (``_hub_db_path``, ``_board_sync_enabled``, ``_meta_float``, ``HUB_STALE_AFTER_S``) are read through
the module at call time, so tests and callers that patch them on ``kanban_session_activity`` keep working."""
from __future__ import annotations

import sqlite3
import time


def replica_task_resolution(ids: list[str], now: float | None = None) -> tuple[set[str], bool]:
    """Which ``t_<hex>`` ids exist as tasks in the hermes-hub replica of any gateway (same file ``_remote_links``
    reads; replica keys are ``[profile/]board/<id>``, so qualified ``@task:p@gw/<id>`` refs resolve by id).

    Returns ``(found, authoritative)``; False when a replica exists but can't answer (unreadable, offline, or not
    synced within ``HUB_STALE_AFTER_S``): absent ids are *unknown*, never flagged. No replica is authoritative only
    when ``kanban.sync`` is off; a Board-synced host (worker node: no hub sync) holds a partial board. [daveey]"""
    from hermes_cli import kanban_session_activity as _ksa

    path = _ksa._hub_db_path()
    if path is None or not ids:  # sync_enabled never raises: an unreadable config reads as sync off (standalone)
        return set(), not ids or not _ksa._board_sync_enabled()
    try:
        from hermes_cli.kanban_board_replica_refs import hub_tasks_found, with_reader

        def read(conn: sqlite3.Connection) -> tuple:
            conn.execute("BEGIN")  # one snapshot: the task-row probe must describe the rows the lookups read
            try:
                meta = _ksa._meta_float
                offline = meta(conn, "offline_since")
                synced = max(filter(None, (meta(conn, "last_ok"), meta(conn, "last_digest"))), default=None)
                return offline, synced, hub_tasks_found(conn, path, ids)
            finally:
                conn.execute("ROLLBACK")

        offline, synced, found = with_reader(path, read)  # kept reader: ~2 ms an open on every board GET
    except sqlite3.Error:
        return set(), False
    now = time.time() if now is None else now
    fresh = offline is None and synced is not None and now - synced <= _ksa.HUB_STALE_AFTER_S
    return found, fresh
