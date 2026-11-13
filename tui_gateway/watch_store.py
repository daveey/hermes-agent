"""Kept read-only observer connections for the change watcher's store signatures. [daveey]

Design t_01a1d563324d7e8e §3 card J. ``groups.changed`` and ``sessions.changed`` re-read state.db on every pass
whose DB/WAL mtime moved, and on a busy hub state.db commits on most 0.5 s passes: each re-read opened a tracked
connection (schema parse), resolved the path and closed it again (about 4 ms and 3 ms of CPU per pass). A store
now keeps ONE read-only autocommit connection. Its key is the file identity plus ``PRAGMA data_version``, which
moves on every commit by any other connection, this process's writers included, and is exact where an mtime is
coarse. The main file's (size, mtime_ns) is in the key too: a restore copied over the same inode moves it.

A kept connection is closed and reopened when the store's (dev, ino) changes, when a ``-wal``/``-shm`` it was
opened against has a new identity (a new WAL generation: a handle stranded on a deleted one would make the next
writer refuse to open, ``refuse_deleted_wal_generation``), when the store is gone, and by
``hermes_state_registry.close_all_under`` (profile delete, snapshot restore) through :func:`release_under`.
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
# path -> [(dev, ino), conn, generation, {sidecar suffix: (dev, ino)}]. A new generation never equals an old key,
# so a reopened store re-reads.
_held: OrderedDict[str, list] = OrderedDict()
_generation = 0


def _sidecars(path: str) -> dict:
    from hermes_state_dbfile import _stat_sqlite_sidecar_identity
    return _stat_sqlite_sidecar_identity(Path(path))


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
    """Close the kept connection of a store that is gone."""
    with _lock:
        _drop(str(db_path))


def release_under(root=None) -> int:
    """Close every kept connection on a store under ``root`` (every one when None): called by
    ``hermes_state_registry.close_all``/``close_all_under`` before the header probes are released, so a profile
    delete's rmtree meets no kept descriptor. The next watch pass reopens what still exists."""
    from hermes_state_common import path_is_under
    with _lock:
        doomed = [path for path in _held if root is None or path_is_under(Path(path), Path(root))]
        for path in doomed:
            _drop(path)
    return len(doomed)


def _open(path: str, ident: tuple[int, int]) -> list:
    global _generation
    from hermes_state import _connect_tracked_db
    from hermes_state_holders import read_only_db_uri
    # Tracked: a kept fd must be visible to has_live_connection()/offline_file_access() like every state.db opener.
    conn = _connect_tracked_db(read_only_db_uri(Path(path)), tracking_path=Path(path), uri=True,
                               check_same_thread=False, isolation_level=None, timeout=0.05)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA data_version").fetchone()  # attach to the current WAL generation before recording it
        st = os.stat(path)
        if (st.st_dev, st.st_ino) != ident:
            raise sqlite3.OperationalError("store replaced while its observer opened")
        sidecars = _sidecars(path)
    except BaseException:
        conn.close()
        raise
    _generation += 1
    held = _held[path] = [ident, conn, _generation, sidecars]
    while len(_held) > MAX_STORES:
        _drop(next(iter(_held)))
    return held


def _stranded(held: list, path: str) -> bool:
    """A sidecar the connection was opened against now names another file (or none): a new WAL generation."""
    now = _sidecars(path)
    if any(now.get(suffix) != ident for suffix, ident in held[3].items()):
        return True
    held[3] = {**now, **held[3]}  # a sidecar created since the open is this connection's generation
    return False


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
            if held is not None and (held[0] != ident or _stranded(held, path)):
                _drop(path)  # replaced, recreated or a new WAL generation under this path
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
