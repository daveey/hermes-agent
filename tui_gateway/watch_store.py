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
# No cross-connection data_version comparison: consumers must read semantic content.
TRANSIENT = object()
_lock = threading.RLock()
# path -> [(dev, ino), conn, generation, {sidecar suffix: (dev, ino)}]. A new generation never equals an old key,
# so a reopened store re-reads.
_held: OrderedDict[str, list] = OrderedDict()
_generation = 0
_released_roots: set[Path] = set()
_release_all = False
_reactivated: set[str] = set()


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
    global _release_all
    with _lock:
        while _held:
            _drop(next(iter(_held)))
        _released_roots.clear()
        _reactivated.clear()
        _release_all = False


def forget(db_path) -> None:
    """Close the kept connection of a store that is gone."""
    with _lock:
        _drop(str(db_path))


def release_under(root=None) -> int:
    """Close every kept connection on a store under ``root`` (every one when None): called by
    ``hermes_state_registry.close_all``/``close_all_under`` before the header probes are released, so a profile
    delete's rmtree meets no kept descriptor. Later idle passes use transient readers until
    another tracked owner has reopened the store."""
    global _release_all
    from hermes_state_common import path_is_under
    with _lock:
        if root is None:
            _release_all = True
            _released_roots.clear()
            _reactivated.clear()
        else:
            root = Path(root).resolve()
            _reactivated.difference_update({p for p in _reactivated if path_is_under(Path(p), root)})
            if not _release_all:
                _released_roots.difference_update({p for p in _released_roots if path_is_under(p, root)})
                if not any(path_is_under(root, p) for p in _released_roots):
                    _released_roots.add(root)
        doomed = [path for path in _held if root is None or path_is_under(Path(path), root)]
        for path in doomed:
            _drop(path)
    return len(doomed)


def _transient(path: str) -> bool:
    from hermes_state_common import path_is_under
    from hermes_cli.sqlite_safe_read import has_live_connection
    if path in _reactivated:
        return False
    released = _release_all or any(path_is_under(Path(path), root) for root in _released_roots)
    if not released:
        return False
    # Check before opening our own tracked observer, which must never reactivate itself.
    if has_live_connection(path):
        _reactivated.add(path)
        return False
    return True


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
    held = _held[path] = [ident, conn, _generation, sidecars, (st.st_size, st.st_mtime_ns)]
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
    """``(key, conn)`` under the module lock; non-TRANSIENT equal keys permit signature reuse.
    Retained readers use connection-local commit counters. Released ownerless readers yield
    TRANSIENT: consumers read semantic content, never compare counters across fresh connections.
    ``conn`` is autocommit; a caller wanting one snapshot over several statements wraps them in BEGIN/COMMIT and
    must not leave a transaction open. Raises OSError/sqlite3.Error when the store cannot be read; any error
    inside the block drops the connection."""
    path = str(db_path)
    with _lock:
        transient = _transient(path)
        try:
            st = os.stat(path)
            ident = (st.st_dev, st.st_ino)
            held = _held.get(path)
            if held is not None and (held[0] != ident or _stranded(held, path)
                                     or held[4] != (st.st_size, st.st_mtime_ns)):
                _drop(path)  # replaced, recreated or a new WAL generation under this path
                held = None
            if held is None:
                held = _open(path, ident)
            _held.move_to_end(path)
            conn = held[1]
            if transient:
                key = TRANSIENT
            else:
                version = conn.execute("PRAGMA data_version").fetchone()[0]
                key = (*ident, held[2], st.st_size, st.st_mtime_ns, version)
        except BaseException:
            _drop(path)
            raise
        try:
            yield key, conn
        except BaseException:
            _drop(path)
            raise
        if transient or conn.in_transaction:  # an idle released store must not regain a kept descriptor
            _drop(path)
