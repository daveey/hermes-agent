"""Close this process's kept handles on boards whose files were removed. [daveey]

Several process-wide caches keep a connection (or a raw fd) per board file and revalidate it only when that same
path is read again: the sync loop's ``BoardWatch``, the shared ``cached_ro`` readers, the per-thread
``kanban_kept_reader`` connections and ``sqlite_safe_read``'s pread fds. Once a whole board directory is removed,
nothing reads that path again, so the kept handle pinned the deleted ``kanban.db``, ``-wal`` and ``-shm`` inodes
until the process exited (hermes-hub 2026-11-13: two gateways held the removed probe board
``auw-probe-t01a1d6ae`` 30+ min after its removal; ``stranded_sqlite_watch`` filed a card per gateway).

:func:`sweep` closes every idle kept handle whose path no longer exists. It is cheap (one ``stat`` per kept
handle) and runs from the sync loop every round and from gateway housekeeping every tick. A cache whose module
was never imported holds nothing, so it is skipped without importing it. Closing such a handle cannot cancel
another connection's locks on a live board: the file it opened is gone, and SQLite closes an inode's descriptors
only with its last connection in this process.
"""
from __future__ import annotations

import logging
import sqlite3
import sys

_log = logging.getLogger(__name__)


def sweep() -> int:
    """Close idle kept handles on removed board files; returns how many connections closed."""
    closed = 0
    client = sys.modules.get("hermes_cli.kanban_sync_client")
    if client is not None:
        closed += client._WATCH.release_gone()
    if "hermes_cli.kanban_session_activity" in sys.modules:
        from hermes_cli.kanban_reader_retention import release_gone as release_shared
        closed += _guarded(release_shared)
    kept = sys.modules.get("hermes_cli.kanban_kept_reader")
    if kept is not None:
        closed += _guarded(kept.release_gone)
    safe_read = sys.modules.get("hermes_cli.sqlite_safe_read")
    if safe_read is not None:
        safe_read.release_unlinked_fds()
    if closed:
        _log.info("kanban: closed %d kept connection(s) on removed board files", closed)
    return closed


def _guarded(release) -> int:
    """One cache's sweep; a SQLite error closing a dead handle must not stop the others."""
    try:
        return release()
    except sqlite3.Error as exc:
        _log.debug("kanban: kept-handle sweep failed: %s", exc)
        return 0
