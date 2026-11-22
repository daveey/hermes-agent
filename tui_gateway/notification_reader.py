"""Notification-only read observers; unrelated dashboard borrows never cool their memos.

One serialized retained observer per board, bounded to 64 boards. Contention within
this domain gets a fresh private read, never a memo from another connection.
Generation recovery uses nonblocking eviction: never close a foreign borrower or
wait for its lock while holding the board lifecycle lock.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

from hermes_cli import kanban_generation as generation

MAX_READERS = 64
BUSY_BUDGET = 0.5
SHARED_WAIT = 0.05
_registry = OrderedDict()
_registry_lock = threading.Lock()


class _Reader:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.conn: sqlite3.Connection | None = None
        self.ident: tuple[int, int] | None = None
        self.wal: tuple[int, int] | None = None
        self.retired = False

    def close(self):
        self.retired = True
        if self.conn is not None:
            # Guarded close takes this lock internally, without a timeout. Take
            # it nonblocking first; preserve custody if another opener owns it.
            with generation.lifecycle_access(self.path, timeout=0):
                self.conn.close()
                self.conn = None


def _identity(path):
    try:
        st = os.stat(path)
        return st.st_dev, st.st_ino
    except OSError:
        return None


def _live(reader, path):
    from hermes_cli.kanban_lockguard import shm_identity
    conn = reader.conn
    return (not reader.retired and conn is not None and conn in generation._connections
            and path not in generation._poisoned and reader.ident == _identity(path)
            and reader.wal == _identity(path + '-wal')
            and getattr(conn, '_hermes_generation_shm', None) == shm_identity(path))


def evict(path):
    """Generation recovery hook: no waiting under its lifecycle lock."""
    with _registry_lock:
        reader = _registry.get(path)
    if reader is None or reader.conn is None:
        return
    if not reader.lock.acquire(blocking=False):
        raise generation.DeletedWalGenerationError('notification reader is borrowed; retry generation recovery')
    try:
        reader.close()
    finally:
        reader.lock.release()


generation.register_reader_cache(evict)


def close_all():
    """Retire all readers; active/busy readers retain custody until they can close."""
    with _registry_lock:
        readers = list(_registry.values())
        for reader in readers:
            reader.retired = True
    for reader in readers:
        if reader.lock.acquire(blocking=False):
            try:
                with contextlib.suppress(generation.BoardLifecycleBusy):
                    reader.close()
                if reader.conn is None:
                    with _registry_lock:
                        if _registry.get(reader.path) is reader:
                            del _registry[reader.path]
            finally:
                reader.lock.release()


def _reader(path):
    with _registry_lock:
        reader = _registry.get(path)
        if reader is not None:
            _registry.move_to_end(path)
            return reader
        candidates = list(_registry.values()) if len(_registry) >= MAX_READERS else []
    for old in candidates:
        if not old.lock.acquire(blocking=False):
            continue
        try:
            with contextlib.suppress(generation.BoardLifecycleBusy):
                old.close()
            if old.conn is None:
                with _registry_lock:
                    if _registry.get(old.path) is old:
                        del _registry[old.path]
                break
        finally:
            old.lock.release()
    with _registry_lock:
        if path in _registry:
            return _registry[path]
        if len(_registry) >= MAX_READERS:
            return None  # every slot still owned: private, never exceed the cap
        reader = _registry[path] = _Reader(path)
        return reader


@contextlib.contextmanager
def reading(path, *, busy_budget=BUSY_BUDGET):
    """Yield (connection, retained custody), exclusive through signature publication.

    The coordination/open budget is shared, not renewed after waiting. A private
    fallback is query-only and always closes, even on non-SQL exceptions.
    """
    path = Path(path)
    key = generation._path(path)
    started = time.monotonic()
    reader = _reader(key)
    acquired = reader is not None and reader.lock.acquire(timeout=max(0.0, min(SHARED_WAIT, busy_budget)))
    if not acquired:
        with contextlib.closing(generation.open_ro(path, timeout=max(0.0, busy_budget - (time.monotonic() - started)))) as conn:
            yield conn, False
        return
    assert reader is not None and key is not None
    with _registry_lock:
        attached = _registry.get(key) is reader
    try:
        if not _live(reader, key):
            reader.close()
            before = _identity(key)
            reader.retired = False
            reader.conn = generation.open_ro(path, check_same_thread=False,
                                             timeout=max(0.0, busy_budget - (time.monotonic() - started)))
            reader.ident = before
            reader.wal = _identity(key + '-wal')
            if before is None or before != _identity(key):
                reader.close()
                raise generation.DeletedWalGenerationError('notification board replaced during open; retry')
        conn = reader.conn
        assert conn is not None
        try:
            yield conn, attached and not reader.retired
        except sqlite3.Error:
            reader.close()
            raise
        finally:
            if reader.conn is not None and (not attached or reader.retired or reader.conn.in_transaction):
                reader.close()
    finally:
        reader.lock.release()
