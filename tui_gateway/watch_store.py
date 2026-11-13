"""Kept read-only observer connections for the change watcher's store signatures. [daveey]

Design t_01a1d563324d7e8e §3 card J. ``groups.changed`` and ``sessions.changed`` re-read state.db on every pass
whose DB/WAL mtime moved, and on a busy hub state.db commits on most 0.5 s passes: each re-read opened a tracked
connection (schema parse), resolved the path and closed it again (about 4 ms and 3 ms of CPU per pass). A store
now keeps ONE read-only autocommit connection. Its key is the file identity plus ``PRAGMA data_version``, which
moves on every commit by any other connection, this process's writers included, and is exact where an mtime is
coarse. The main file's (size, mtime_ns) is in the key too: a restore copied over the same inode moves it.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path

MAX_STORES = 32
_lock = threading.RLock()
# path -> ((dev, ino), conn, generation). A new generation never equals an old key, so a reopened store re-reads.
_held: OrderedDict[str, tuple[tuple[int, int], sqlite3.Connection, int]] = OrderedDict()
_generation = 0


def _drop(path: str) -> None:
    held = _held.pop(path, None)
    if held is not None:
        with contextlib.suppress(sqlite3.Error):
            held[1].close()


def reset() -> None:
    """Close every kept connection (tests, and a process forgetting its memos)."""
    with _lock:
        while _held:
            _drop(next(iter(_held)))


def forget(db_path) -> None:
    """Close the kept connection of a store that is gone: a held fd on its deleted WAL would make the next
    writer that recreates it refuse to open (``refuse_deleted_wal_generation``)."""
    with _lock:
        _drop(str(db_path))


def _open(path: str, ident: tuple[int, int]) -> tuple:
    global _generation
    from hermes_state import _connect_tracked_db
    from hermes_state_holders import read_only_db_uri
    # Tracked: a kept fd must be visible to has_live_connection()/offline_file_access() like every state.db opener.
    conn = _connect_tracked_db(read_only_db_uri(Path(path)), tracking_path=Path(path), uri=True,
                               check_same_thread=False, isolation_level=None, timeout=0.05)
    try:
        conn.execute("PRAGMA query_only = ON")
        st = os.stat(path)
        if (st.st_dev, st.st_ino) != ident:
            raise sqlite3.OperationalError("store replaced while its observer opened")
    except BaseException:
        conn.close()
        raise
    _generation += 1
    held = _held[path] = (ident, conn, _generation)
    while len(_held) > MAX_STORES:
        _drop(next(iter(_held)))
    return held


@contextlib.contextmanager
def observed(db_path):
    """``(key, conn)`` for ``db_path`` under the module lock: equal keys mean no commit was published between them.
    ``conn`` is autocommit; a caller wanting one snapshot over several statements wraps them in BEGIN/COMMIT and
    must not leave a transaction open. Raises OSError/sqlite3.Error when the store cannot be read; any error
    inside the block drops the connection, so the next call reopens (a new generation: no stale key matches)."""
    path = str(db_path)
    with _lock:
        try:
            st = os.stat(path)
            ident = (st.st_dev, st.st_ino)
            held = _held.get(path)
            if held is not None and held[0] != ident:
                _drop(path)  # replaced or recreated under this path
                held = None
            if held is None:
                held = _open(path, ident)
            _held.move_to_end(path)
            conn = held[1]
            version = conn.execute("PRAGMA data_version").fetchone()[0]
        except BaseException:
            _drop(path)
            raise
        try:
            yield (*ident, held[2], st.st_size, st.st_mtime_ns, version), conn
        except BaseException:
            _drop(path)
            raise
        if conn.in_transaction:  # a caller's snapshot left open would pin the WAL and hide later commits
            _drop(path)
