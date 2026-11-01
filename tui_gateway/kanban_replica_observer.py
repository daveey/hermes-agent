"""Change keys for the hub replica from retained read-only SQLite observers. [daveey]

An idle notice poll asks one retained connection per replica for ``PRAGMA data_version`` instead of reading the
replica. A file stat is not a change key: a WAL commit appends and syncs its frames before it publishes them in
the -shm index, so a stat taken in that gap pairs the new files with a read of the old data, and nothing the stat
covers changes when the commit becomes visible (594 of 1000 commits from another process were never read).
``data_version`` moves with the same visibility a read has, but only within one connection, so every key carries
the generation of the connection that produced it.
"""
from __future__ import annotations

import itertools
import os
import sqlite3
import threading
from collections import OrderedDict
from typing import NamedTuple, Optional
from urllib.parse import quote

MAX_OBSERVERS = 16

_lock = threading.Lock()
_observers: OrderedDict[str, tuple] = OrderedDict()  # resolved path -> (connection, (dev, inode), generation)
_generations = itertools.count(1)


class Fingerprint(NamedTuple):
    path: str
    ident: tuple  # (dev, inode) of the db file the observer holds open
    generation: int  # which observer connection measured data_version
    data_version: int
    signature: tuple  # db file (size, mtime_ns): also invalidates on a restore written under the same inode


def fingerprint(path) -> Optional[Fingerprint]:
    """The replica's change key, or None when unknown (read it and keep nothing that would skip the next poll).

    Equal keys mean no commit was published between them. A missing, replaced or failing replica closes its
    observer; the next call opens a new one (a new generation, so its keys never equal the old ones).
    """
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return None
    with _lock:
        try:
            st = os.stat(real)
            ident = (st.st_dev, st.st_ino)
            held = _observers.get(real)
            if held is not None and held[1] != ident:
                _evict(real)  # replaced or recreated under this path
                held = None
            if held is None:
                held = _open(real, ident)
            _observers.move_to_end(real)
            version = _data_version(held[0])
        except (OSError, ValueError, sqlite3.Error):
            _evict(real)
            return None
    return Fingerprint(real, ident, held[2], version, (st.st_size, st.st_mtime_ns))


def file_identity(stamp: Optional[Fingerprint]) -> Optional[tuple]:
    """``(path, dev, inode)``: equal identity means the same db file (possibly rewritten), whichever observer."""
    return (stamp.path, *stamp.ident) if stamp is not None else None


def reset() -> None:
    """Close every observer (tests, and a backend forgetting its memos)."""
    with _lock:
        while _observers:
            _evict(next(iter(_observers)))


def _open(real: str, ident: tuple) -> tuple:
    conn = sqlite3.connect(f"file:{quote(real)}?mode=ro", uri=True, timeout=1.0, check_same_thread=False,
                           isolation_level=None)  # autocommit: no statement leaves a read transaction open
    try:
        conn.execute("PRAGMA query_only = ON")
        st = os.stat(real)
        if (st.st_dev, st.st_ino) != ident:
            raise sqlite3.OperationalError("replica replaced while its observer opened")
    except BaseException:
        conn.close()
        raise
    held = _observers[real] = (conn, ident, next(_generations))
    while len(_observers) > MAX_OBSERVERS:
        _evict(next(iter(_observers)))
    return held


def _data_version(conn: sqlite3.Connection) -> int:
    rows = conn.execute("PRAGMA data_version").fetchall()  # stepped to completion: the read lock is released
    if conn.in_transaction:
        raise sqlite3.OperationalError("observer left a transaction open")
    return int(rows[0][0])


def _evict(real: str) -> None:
    held = _observers.pop(real, None)
    if held is not None:
        try:
            held[0].close()
        except sqlite3.Error:
            pass
