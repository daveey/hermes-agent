"""Bounded SQLite reuse owned by one sync loop, never by the process.

Only idle connections are retained; callers still own their transactions. The existing
Kanban opener supplies all configuration and lockguard tracking. Keeping the connection
also keeps SQLite's prepared statement cache, without sharing it with another thread.
"""

from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
from typing import Callable, Iterator


_local = threading.local()
DEFAULT_LIMIT = 32  # retained idle handles; sync_board additionally borrows at most live + shadow


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
        return st.st_dev, st.st_ino
    except OSError:
        return None


def _idle(conn: sqlite3.Connection) -> bool:
    try:
        return not conn.in_transaction
    except sqlite3.ProgrammingError:
        return False  # same-thread WAL generation recovery may already have closed it


class _Pool:
    def __init__(self, limit: int):
        self.limit = limit
        self.idle: OrderedDict[Path, tuple[sqlite3.Connection, tuple[int, int] | None]] = OrderedDict()

    def close(self) -> None:
        while self.idle:
            self.idle.popitem()[1][0].close()

    def take(self, path: Path, opener: Callable[[], sqlite3.Connection]) -> sqlite3.Connection:
        # A retained handle must pass the same WAL-generation fence as a fresh
        # opener. Recovery can close same-thread idle handles, including ours.
        from hermes_cli.kanban_generation import recover
        recover(path)
        entry = self.idle.pop(path, None)
        if entry is not None:
            conn, identity = entry
            if identity is not None and identity == _identity(path) and _idle(conn):
                return conn
            conn.close()
        return opener()

    def put(self, path: Path, conn: sqlite3.Connection, identity: tuple[int, int] | None) -> None:
        old = self.idle.pop(path, None)
        if old is not None:
            old[0].close()
        while len(self.idle) >= self.limit:
            self.idle.popitem(last=False)[1][0].close()
        self.idle[path] = conn, identity


@contextmanager
def connection_scope(limit: int = DEFAULT_LIMIT) -> Iterator[None]:
    """Opt in for this thread and lifetime; nested loops get independent ownership."""
    if limit < 1:
        raise ValueError("connection limit must be positive")
    previous = getattr(_local, "pool", None)
    pool = _Pool(limit)
    _local.pool = pool
    try:
        yield
    finally:
        _local.pool = previous
        pool.close()


@contextmanager
def connection(path: Path, opener: Callable[[], sqlite3.Connection]) -> Iterator[sqlite3.Connection]:
    """Borrow exclusively, or close per call outside a loop. Failed work is never cached."""
    path = path.absolute()
    pool = getattr(_local, "pool", None)
    conn = pool.take(path, opener) if pool is not None else opener()
    identity = _identity(path)
    completed = False
    try:
        yield conn
        completed = True
    finally:
        if (pool is not None and completed and _idle(conn)
                and identity is not None and identity == _identity(path)):
            pool.put(path, conn, identity)
        else:
            conn.close()  # SQLite rolls back any unfinished transaction; never commit it here.
            if pool is not None and not completed:
                pool.close()  # A later push can fail after the shadow was already returned.


def invalidate(path: Path) -> None:
    """Close an idle cached handle before replacing its file (e.g. retention resync)."""
    pool = getattr(_local, "pool", None)
    if pool is not None:
        entry = pool.idle.pop(path.absolute(), None)
        if entry is not None:
            entry[0].close()
