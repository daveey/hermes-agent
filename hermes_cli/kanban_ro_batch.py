"""One read-only connection per file for the length of one read pass (``kanban_origin._ro``). [daveey]

A Desktop task-list read (``session_tasks``) asks about every card it lists, and each question opened its own
read-only connection through ``kanban_lockguard`` (lifecycle lock, recovery probe, WAL guard, fd scan) and closed
it again: ~195 opens of the same few files per 12 calls on the hub, about 1 s of CPU (2026-11-20). Inside
``batch()`` the first ``_ro(path)`` opens the file and later ones borrow that same connection; the batch closes it
when the pass ends.

A borrow behaves like a fresh connection to its user: ``close()`` ends the borrow (rolls back a transaction the
borrower left open, resets ``row_factory``) instead of closing the file. A second ``_ro`` of a file whose
connection is still borrowed (a nested reader, or another thread that inherited the context) gets its own private
connection, exactly as without a batch, so one borrower's ``BEGIN``/``close`` never touches another's snapshot.
An autocommit reader holds no snapshot between statements, so a shared connection reads what a fresh one would.
"""
from __future__ import annotations

import contextlib
import contextvars
import sqlite3
import threading
from typing import Any, Callable, Iterator, Optional

_active: contextvars.ContextVar[Optional[_Batch]] = contextvars.ContextVar("kanban_ro_batch", default=None)


class _Batch:
    def __init__(self) -> None:
        self.thread = threading.get_ident()
        self.conns: dict[str, sqlite3.Connection] = {}
        self.borrowed: set[str] = set()
        self.closed = False

    def close(self) -> None:
        self.closed = True  # a context copied inside the batch and run later on this thread must not reuse it
        conns, self.conns = self.conns, {}
        for conn in conns.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass


class _Borrowed:
    """A borrow of a batch's connection; every attribute but ``close`` is the connection's own."""

    __slots__ = ("_batch", "_conn", "_key")

    def __init__(self, conn: sqlite3.Connection, batch: _Batch, key: str) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_batch", batch)
        object.__setattr__(self, "_key", key)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_conn"), name, value)

    def __enter__(self) -> _Borrowed:
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: object) -> Any:
        return self._conn.__exit__(*exc)

    def close(self) -> None:
        batch = object.__getattribute__(self, "_batch")
        if batch is None:
            return
        object.__setattr__(self, "_batch", None)
        conn, key = self._conn, self._key
        try:
            if conn.in_transaction:
                conn.rollback()
            conn.row_factory = None
        except sqlite3.Error:
            # A connection that cannot be returned clean is not shared again.
            if batch.conns.get(key) is conn:
                del batch.conns[key]
            try:
                conn.close()
            except sqlite3.Error:
                pass
        batch.borrowed.discard(key)


@contextlib.contextmanager
def batch() -> Iterator[None]:
    """Share one read-only connection per file among ``_ro`` calls in this block; nested blocks join the outer.

    A batch inherited from another thread (a background refresh started with ``copy_context()`` inside a batched
    read) is not joined: its connections belong to that thread. This block opens its own."""
    outer = _active.get()
    if outer is not None and not outer.closed and outer.thread == threading.get_ident():
        yield
        return
    current = _Batch()
    token = _active.set(current)
    try:
        yield
    finally:
        _active.reset(token)
        current.close()


def _usable(conn: sqlite3.Connection) -> bool:
    """False once something closed the idle shared connection behind the batch's back: opening another handle to
    the same board runs ``kanban_generation.recover``, which closes this process's handles on a replaced WAL/SHM
    generation."""
    try:
        _ = conn.total_changes  # raises ProgrammingError on a closed connection
    except sqlite3.ProgrammingError:
        return False
    return True


def open_ro(path: Any, opener: Callable[[Any], sqlite3.Connection]) -> sqlite3.Connection:
    """``opener(path)``, or a borrow of this batch's connection to ``path`` (opened by ``opener`` once)."""
    current = _active.get()
    if current is None or current.closed or current.thread != threading.get_ident():
        return opener(path)
    key = str(path)
    if key in current.borrowed:
        return opener(path)
    conn = current.conns.get(key)
    if conn is not None and not _usable(conn):
        del current.conns[key]
        conn = None
    if conn is None:
        conn = current.conns[key] = opener(path)
    current.borrowed.add(key)
    return _Borrowed(conn, current, key)  # type: ignore[return-value]
