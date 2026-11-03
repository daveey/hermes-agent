"""Cards this gateway holds only as a Board replica copy: read them there, refuse writes naming the owner. [daveey]

On a synced board the live ``kanban.db`` holds only this gateway's own cards (plus copies the worker-node pickup
made); the replica (``kanban/sync-shadow/<board>.db``) holds the whole board (``hermes_cli.kanban_worker_node``).
A card another gateway created (a Board worker node such as worker-1, which no Desktop can reach) therefore read as
404 on every gateway, and a write on it was refused as ``unknown id`` / ``stale_run``, so a chat checking a worker's
named follow-up concluded the card was a phantom (t_01a19fd40c53bb19, 2026-11-02).

:func:`open_replica` opens the replica read-only when the board syncs. :func:`owner_of` answers whether a card
exists only there and who created it; :func:`refusal` is the one message every write path returns for it, tagged
with the stable rejection :data:`REJECTION`.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

_log = logging.getLogger(__name__)

REJECTION = "foreign_card"
_UNSET = object()


class ForeignCardError(Exception):
    """A write on a card this gateway has only as a replica copy."""

    def __init__(self, task_id: str, owner: Optional[str]):
        self.task_id = task_id
        self.owner = owner
        super().__init__(message(task_id, owner))


def message(task_id: str, owner: Optional[str]) -> str:
    return (f"{task_id} was created on {owner or 'another gateway'} and lives there; this gateway has only a "
            "replica copy. Its owner or the Board must write it.")


def replica_path(db_path: Optional[str]) -> Optional[Path]:
    """The replica of the board whose live file is ``db_path``, when ``kanban.sync`` syncs that board and the
    replica exists; None otherwise (sync off, board not selected, no replica yet, or ``db_path`` is the replica)."""
    if not db_path:
        return None
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as ksc
    config = sync.sync_config()
    if not config.get("enabled") or not sync.board_selected(str(db_path), config):
        return None
    path = ksc.shadow_path(sync.board_slug_for_path(str(db_path)))
    try:
        if not path.is_file() or path.resolve() == Path(db_path).resolve():
            return None
    except OSError:
        return None
    return path


def _ro(path: Path) -> sqlite3.Connection:
    """Read-only open; ``mode=ro`` refuses a WAL db whose -shm is absent, so fall back to ``query_only``."""
    from hermes_cli import kanban_lockguard
    conn = None
    try:
        conn = kanban_lockguard.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.OperationalError:
        if conn is not None:  # opened but the probe failed: close it before the fallback replaces it
            conn.close()
        conn = kanban_lockguard.connect(f"file:{path}?mode=rw", uri=True, timeout=5)
        conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def open_replica(db_path: Optional[str]) -> Iterator[Optional[sqlite3.Connection]]:
    """A read-only connection to the board's replica (see :func:`replica_path`), or None. Closed on exit."""
    path = replica_path(db_path)
    conn = None
    if path is not None:
        try:
            conn = _ro(path)
        except sqlite3.Error as exc:  # an unreadable replica is a miss, as before this fallback existed
            _log.info("kanban: replica %s unreadable (%s)", path, exc)
            conn = None
    try:
        yield conn
    finally:
        if conn is not None:
            conn.close()


def created_on(conn: sqlite3.Connection, task_id: str):
    """The card's ``created_on`` in ``conn`` (None when the column is NULL), or ``_UNSET`` when it has no row."""
    try:
        row = conn.execute("SELECT created_on FROM tasks WHERE id = ?", (task_id,)).fetchone()
    except sqlite3.OperationalError:  # an unsynced schema has no created_on column
        row = conn.execute("SELECT NULL FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _UNSET if row is None else row[0]


def _live_db_file(conn: sqlite3.Connection) -> Optional[str]:
    from hermes_cli.kanban_db_connect import _main_db_file
    return _main_db_file(conn)


def owner_of(conn: sqlite3.Connection, task_id: str) -> tuple[bool, Optional[str]]:
    """``(replica_only, owner)`` for ``task_id`` against the live connection ``conn``.

    ``replica_only`` is True when the live file lacks the card and the board's replica has it; ``owner`` is then
    the replica row's ``created_on``. A card in the live file, or in neither, is ``(False, None)``.
    """
    try:
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
            return False, None
    except sqlite3.Error:
        return False, None
    with open_replica(_live_db_file(conn)) as replica:
        if replica is None:
            return False, None
        try:
            owner = created_on(replica, task_id)
        except sqlite3.Error as exc:
            _log.info("kanban: replica lookup of %s failed (%s)", task_id, exc)
            return False, None
    if owner is _UNSET:
        return False, None
    return True, (None if owner is None else str(owner))


def refusal(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The :func:`message` when ``task_id`` exists only in the replica, else None."""
    only, owner = owner_of(conn, task_id)
    return message(task_id, owner) if only else None


def check(conn: sqlite3.Connection, task_id: str) -> None:
    """Raise :class:`ForeignCardError` when ``task_id`` exists only in the replica."""
    only, owner = owner_of(conn, task_id)
    if only:
        raise ForeignCardError(task_id, owner)
