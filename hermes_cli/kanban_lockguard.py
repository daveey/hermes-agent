"""Kanban board connections whose WAL locks a stray ``close()`` cannot cancel. [daveey]

POSIX advisory locks are per process: any plain ``open()``/``close()`` of ``kanban.db`` and its
``-shm`` inside a process (a corrupt-backup copy, a header read, a tool copying ``~/.hermes``)
cancels every lock SQLite holds there (sqlite.org/howtocorrupt.html §2.2). The next sibling
process that closes the board then believes it is the last user, checkpoints, and unlinks
``-wal``/``-shm``; this process is left on the deleted generation, and every read fails with
``disk I/O error`` (or reports false corruption) until restart (kanban-759104eb56).

``state.db`` solved this with :mod:`hermes_state_lockguard` (OFD locks on SQLite's own
descriptors, which a stray close cannot cancel). This module applies the same guard to every
Kanban connection, read-only ones included:

- :func:`connect` is a drop-in ``sqlite3.connect`` that returns a guarded connection;
- :data:`GuardedConnection` / :func:`guarded_factory` compose with other connection factories
  (``sqlite_safe_read.connect_tracked`` mixes its tracking ``close()`` in on top);
- :func:`guard` (re)applies the guard to an open connection (after a fresh board switches to WAL);
- :func:`stranded_sidecars` / :func:`shm_identity` detect a process already stranded, so cached
  readers can reopen instead of failing (or silently reading stale data) until restart.

Only WAL databases are guarded: a read lock on the SHARED range would block every
rollback-journal writer. The guard is lifted before the handle's own close (``release``), so a
true last close still checkpoints and removes the sidecars normally.
"""

from __future__ import annotations

from contextlib import nullcontext
import logging
import os
import sqlite3
import weakref
from typing import Any, Optional

import hermes_state_lockguard as _lockguard
from hermes_cli.kanban_replace_guard import ProtectedRowReplaceError, ReplaceGuardMixin

logger = logging.getLogger(__name__)


def _db_file(conn: sqlite3.Connection) -> Optional[str]:
    """The on-disk path of ``main`` as SQLite reports it; ``None`` for in-memory/temp dbs."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    if not row or len(row) < 3 or not row[2]:
        return None
    return str(row[2])


def _release(conn: Any) -> None:
    fin = getattr(conn, "_hermes_lockguard_finalizer", None)
    if fin is not None:
        conn._hermes_lockguard_finalizer = None
        fin()  # runs hermes_state_lockguard.release(held) exactly once


_lifecycle_factory_cache: dict[type, type] = {}


def lifecycle_factory(factory: type) -> type:
    # Inherited lifecycle support is not enough: a tracking wrapper may now
    # precede it. Always put this method outside any newly composed wrappers.
    if factory.__dict__.get("_hermes_outer_lifecycle", False):
        return factory
    cached = _lifecycle_factory_cache.get(factory)
    if cached is None:
        class LifecycleConnection(factory):
            _hermes_outer_lifecycle = True

            def close(self) -> None:
                with getattr(self, "_hermes_lifecycle_lock", nullcontext()):
                    super().close()

        cached = LifecycleConnection
        _lifecycle_factory_cache[factory] = cached
    return cached


class _GuardMixin(ReplaceGuardMixin):
    """Lift the OFD guard immediately before the real close (see ``hermes_state_lockguard.release``);
    a connection that is garbage-collected unclosed releases through its finalizer instead."""

    def __init__(self, database, *args, **kwargs):
        from hermes_cli.kanban_generation import _file_identity, _path
        path = _path(database)
        before = _file_identity(path) if path is not None else None
        super().__init__(database, *args, **kwargs)
        # Attest at open, including connect_tracked's composed factory. Never
        # infer this at first memo use: the pathname may already be replaced.
        after = _file_identity(path) if path is not None else None
        self._hermes_open_identity = before if before == after else None

    _hermes_lockguard: Optional[dict] = None
    _hermes_lockguard_finalizer: Any = None

    def close(self) -> None:  # type: ignore[misc]
        held = getattr(self, "_hermes_lockguard", None)
        was_guarded = bool(held)
        _release(self)
        try:
            super().close()  # type: ignore[misc]
            from hermes_cli.kanban_generation import forget
            forget(self)
        except sqlite3.ProgrammingError:
            # Cross-thread close refused: the connection stays open, so put the guard back.
            if was_guarded:
                try:
                    # SQL cannot run on this foreign thread either. The held WAL
                    # guard already proved the mode before release emptied it.
                    _apply(self, held_path=self._hermes_lockguard_path, wal_verified=True)
                except Exception:
                    logger.debug("kanban guard rearm after refused close failed", exc_info=True)
            raise


class GuardedConnection(_GuardMixin, sqlite3.Connection):
    """``sqlite3.Connection`` whose WAL locks survive a stray in-process close."""


_factory_cache: dict[type, type] = {}


def guarded_factory(factory: type = sqlite3.Connection) -> type:
    """Return *factory* with the guard's ``close()`` mixed in (cached per factory)."""
    if factory is sqlite3.Connection:
        return GuardedConnection
    if issubclass(factory, _GuardMixin):
        return factory
    cached = _factory_cache.get(factory)
    if cached is None:
        cached = type(f"Guarded{factory.__name__}", (_GuardMixin, factory), {})
        _factory_cache[factory] = cached
    return cached


def is_guarded(conn: sqlite3.Connection) -> bool:
    return bool(getattr(conn, "_hermes_lockguard", None))


def unguard(conn: sqlite3.Connection) -> None:
    """Lift the guard from an open connection. Needed before ``PRAGMA journal_mode=<non-WAL>`` (or
    ``locking_mode=EXCLUSIVE``) on it: leaving WAL takes EXCLUSIVE on the SHARED range, which this
    process's own OFD read lock refuses (``database is locked``), exactly as it refuses a foreign one."""
    _release(conn)
    if getattr(conn, "_hermes_lockguard", None) is not None:
        conn._hermes_lockguard = None


def _apply(conn: Any, held_path: Optional[str] = None, *, strict=False, wal_verified=False) -> bool:
    # Refused-close rearming may not execute SQL on the foreign thread.
    if wal_verified and not held_path:
        return False
    path = held_path or _db_file(conn)
    if not path:
        return False
    if not wal_verified:
        try:
            # First read attaches -wal/-shm so hold() can use SQLite's own fds.
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        except sqlite3.Error:
            if strict:
                raise
            return False
        if str(mode).lower() != "wal":
            return False
    held = getattr(conn, "_hermes_lockguard", None)
    if held is None or getattr(conn, "_hermes_lockguard_finalizer", None) is None:
        held = {}
    held = _lockguard.hold(path, held)
    if not held:
        return False
    conn._hermes_lockguard = held
    conn._hermes_lockguard_path = path
    if getattr(conn, "_hermes_lockguard_finalizer", None) is None:
        conn._hermes_lockguard_finalizer = weakref.finalize(conn, _lockguard.release, held)
    if hasattr(conn, "_hermes_generation_shm") and conn._hermes_generation_shm is None:
        conn._hermes_generation_shm = shm_identity(path)
    return True


def guard(conn: sqlite3.Connection, *, strict=False) -> bool:
    """(Re)apply the guard to *conn* (idempotent; extends it to a ``-shm`` minted after open).
    Returns whether the connection is guarded. With strict=True SQL errors propagate so an opener
    retries instead of returning a partially configured handle. Otherwise an unguardable connection (rollback
    journal, in-memory, unsupported platform, plain ``sqlite3.Connection`` instance) is left as is."""
    if not isinstance(conn, _GuardMixin) or not _lockguard.supported():
        return False
    try:
        return _apply(conn, strict=strict)
    except Exception as exc:  # the guard is protection, never a new failure mode for the caller
        if strict and isinstance(exc, sqlite3.Error):
            raise
        logger.debug("kanban lock guard unavailable", exc_info=True)
        return False


def connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    """Drop-in ``sqlite3.connect`` for Kanban board files: same arguments, guarded result."""
    from hermes_cli.kanban_generation import lifecycle_access, prepare_connection, recover, retry_open
    from hermes_cli.sqlite_safe_read import _tracking_factory, connect_tracked

    # Preserve sqlite3's legacy positional arguments before composing factories.
    names = ("timeout", "detect_types", "isolation_level", "check_same_thread", "factory", "cached_statements", "uri")
    if len(args) > len(names):
        raise TypeError("connect() takes at most 8 positional arguments")
    for name, value in zip(names, args):
        if name in kwargs:
            raise TypeError(f"connect() got multiple values for argument '{name}'")
        kwargs[name] = value
    database = os.fsdecode(os.fspath(database))
    timeout = kwargs.get("timeout", 5.0)
    kwargs["factory"] = lifecycle_factory(guarded_factory(_tracking_factory(kwargs.get("factory", sqlite3.Connection))))

    def open_once(remaining):
        with lifecycle_access(database, remaining) as lock:
            recover(database)
            # Readers must join the same lifetime/reservation authority as writers.
            # Never wait for that registry while holding the board lifecycle lock.
            conn = connect_tracked(database, wait_for_tracking=False, **kwargs)
            conn._hermes_lifecycle_lock = lock
            try:
                prepare_connection(conn, timeout)
                return conn
            except BaseException:
                conn.close()
                raise

    return retry_open(open_once, timeout)


def _canonical(path: Any) -> str:
    try:
        return os.path.realpath(os.fspath(path))
    except (OSError, TypeError, ValueError):
        return str(path)


def stranded_sidecars(db_path: Any) -> list[str]:
    """Deleted ``-wal``/``-shm`` generations of *db_path* that THIS process still holds open: its
    connections read a dead WAL index and need every handle to the board closed and reopened.
    Linux only (``/proc``); ``[]`` elsewhere or when unreadable."""
    base = _canonical(db_path)
    watched = {base + "-wal", base + "-shm"}
    out: list[str] = []
    try:
        names = os.listdir("/proc/self/fd")
    except OSError:
        return out
    # Lock-safe pread fds (quarantine copies, header probes) are not connections: skip them.
    from hermes_cli.sqlite_safe_read import lock_safe_fds

    skip = lock_safe_fds()
    for name in names:
        if name.isdigit() and int(name) in skip:
            continue
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue
        if target.endswith(" (deleted)") and target[: -len(" (deleted)")] in watched:
            out.append(target)
    return out


def shm_identity(db_path: Any) -> Optional[tuple[int, int]]:
    """``(st_dev, st_ino)`` of the board's live ``-shm`` (``None`` when absent). A cached
    connection opened against one ``-shm`` inode is stranded once the path names another (or none)."""
    try:
        st = os.stat(os.fspath(db_path) + "-shm")
    except (OSError, TypeError):
        return None
    return (st.st_dev, st.st_ino)


__all__ = [
    "GuardedConnection", "connect", "guard", "guarded_factory", "is_guarded", "shm_identity",
    "stranded_sidecars", "unguard",
]
