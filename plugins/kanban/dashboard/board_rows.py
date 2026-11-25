"""Retained SQL rows for GET /board.

``board_snapshot`` re-reads an unchanged board every ``CACHE_TTL_S`` so clock-driven
diagnostics, card ages, config, profiles on disk and the hub replica stay current. Most of
that read is SQL row materialization (list_tasks/from_row, events, runs), which only
changes when the database does. This module keeps the materialized rows and re-runs only
the projection on each refresh.

Key: one retained read-only observer connection per board file. ``PRAGMA data_version``
on a connection changes whenever any *other* connection (this process or another one)
commits, including bookkeeping-only writes (heartbeats, claim renewals, wake-check
state). The observer never writes, so every commit moves it. The version is read before
the rows, so a commit that lands during the read is read again next time. A new observer
(first use, file replaced, error, idle expiry, eviction) gets a new generation, so a fresh
connection's counter can never match rows read under an older one. File identity
(dev, ino) and ``PRAGMA schema_version`` are in the key too.

Writes that bypass SQLite (overwriting the file bytes in place) are not covered; neither
were they by the stat signature (kanban_db_connect handles a replaced file on connect).

WAL generations: an observer is a guarded, registered board handle (``kanban_generation.open_ro``
via ``kanban_lockguard.connect``), and ``evict`` is a registered reader cache. When an external
process unlinks the board's ``-wal``/``-shm`` (after a stray close dropped this process's locks),
an observer would otherwise keep the deleted generation mapped: it reads a frozen
``data_version`` (stale rows forever on a continuously polled board) and makes core ``recover()``
refuse every board connect in the process. Two exits close it: each ``version`` compares the
observer's recorded ``-shm`` identity with the live one and drops it on a change, and core
``recover()`` evicts it before any other connect. Eviction never waits on a borrowed observer (it
raises ``DeletedWalGenerationError``; the caller retries), and observers are only closed while
holding their own lock, so a poll mid-PRAGMA never has its connection closed under it.
"""

from __future__ import annotations

import itertools
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from plugins.kanban.dashboard import board_legacy
from typing import Callable, Optional, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

MAX_OBSERVERS = 16  # board files with a retained observer (GET /boards observes every live board)
MAX_ENTRIES = 8  # materialized row sets (board x query params)
OBSERVER_IDLE_S = 600.0  # an observer unused this long is closed (archived/removed boards)

_lock = threading.Lock()
_generation = itertools.count(1)


class _Observer:
    __slots__ = ("conn", "gen", "ident", "lock", "path", "pid", "refresh", "shm", "used")

    def __init__(self, path: str, conn: sqlite3.Connection, ident: tuple, shm: Optional[tuple] = None):
        self.path = path
        self.conn = conn
        self.ident = ident
        self.shm = shm
        self.gen = next(_generation)
        self.pid = os.getpid()
        self.used = time.monotonic()
        self.lock = threading.Lock()  # borrows ``conn`` (a few PRAGMAs); close() waits for it
        self.refresh = threading.Lock()  # serializes ``incremental`` builds; never guards ``conn``

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error as exc:
            log.debug("closing board rows observer for %s: %s", self.path, exc)


_observers: OrderedDict[str, _Observer] = OrderedDict()
_entries: OrderedDict[tuple, tuple] = OrderedDict()  # key -> (version, materialized)


def _ident(path: str) -> tuple:
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def _shm_identity(path: str) -> Optional[tuple]:
    from hermes_cli.kanban_lockguard import shm_identity

    return shm_identity(path)


def _drop_observer(path: str) -> Optional[_Observer]:
    """Caller holds ``_lock``. Unpublish the observer and forget rows read under it; the caller
    closes the returned observer with ``_retire`` AFTER releasing ``_lock``."""
    obs = _observers.pop(path, None)
    for key in [k for k in _entries if k[0] == path]:
        del _entries[key]
    return obs


def _retire(*observers: Optional[_Observer]) -> None:
    """Close unpublished observers, each under its own lock (never with ``_lock`` held): a borrower
    finishes its PRAGMAs before its connection is closed."""
    for obs in observers:
        if obs is not None:
            with obs.lock:
                obs.close()


def _open_reader(path: str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    from hermes_state_guard import ensure_kanban_test_isolation
    from hermes_cli.kanban_generation import open_ro

    ensure_kanban_test_isolation(Path(path))
    # Guarded and registered (recover() runs first, remember() records its -shm generation): never
    # an unregistered handle that could pin a deleted generation where recover() cannot retire it.
    return open_ro(Path(path), check_same_thread=check_same_thread)


def _open_observer(path: str, ident: tuple) -> _Observer:
    conn = _open_reader(path, check_same_thread=False)
    try:
        if _ident(path) != ident:  # replaced between stat and open
            raise sqlite3.OperationalError("board file replaced while opening observer")
        shm = getattr(conn, "_hermes_generation_shm", None) or _shm_identity(path)
    except BaseException:
        conn.close()
        raise
    return _Observer(path, conn, ident, shm)


def evict(path: str) -> None:
    """``kanban_generation`` reader-cache hook: close every observer of ``path`` (a realpath) before
    core reopens the board on a new WAL generation. Never waits (recover() may run with other cache
    locks held): a borrowed observer raises ``DeletedWalGenerationError`` and the caller retries."""
    from hermes_cli.kanban_generation import DeletedWalGenerationError

    # Unpublish under ``_lock`` but close only after releasing it: closing a board connection takes
    # that board's lifecycle lock, and a locked connect() holds the lifecycle lock while recover()
    # calls this hook (lock-order inversion otherwise). Each observer stays under its own lock.
    taken: list = []
    try:
        with _lock:
            for key in [p for p in _observers if os.path.realpath(p) == path]:
                obs = _observers[key]
                if not obs.lock.acquire(blocking=False):
                    raise DeletedWalGenerationError(f"kanban DB {path}: board rows observer is borrowed; retry")
                taken.append(obs)
                _drop_observer(key)
    finally:
        for obs in taken:
            try:
                obs.close()
            finally:
                obs.lock.release()


from hermes_cli.kanban_generation import register_reader_cache as _register_reader_cache

_register_reader_cache(evict)


def _sweep(now: float) -> list:
    """Caller holds ``_lock``; returns the observers to ``_retire`` after releasing it."""
    out = [_drop_observer(p) for p in
           [p for p, o in _observers.items() if now - o.used > OBSERVER_IDLE_S or o.pid != os.getpid()]]
    while len(_observers) > MAX_OBSERVERS:
        out.append(_drop_observer(next(iter(_observers))))
    return out


def _observer(path: str) -> Optional[_Observer]:
    """Acquire current observer custody without redundantly sampling its clock."""
    path = str(path)
    now = time.monotonic()
    try:
        ident = _ident(path)
    except OSError:
        with _lock:
            gone = _drop_observer(path)
        _retire(gone)
        return None
    shm = _shm_identity(path)
    with _lock:
        retired = _sweep(now)
        obs = _observers.get(path)
        if obs is not None and (obs.ident != ident or obs.shm != shm):
            # Replaced file, or its WAL generation is gone/new: this handle may map a deleted -shm and
            # read a frozen data_version. Drop it; reopening goes through recover().
            retired.append(_drop_observer(path))
            obs = None
    _retire(*retired)
    if obs is None:
        try:
            fresh = _open_observer(path, ident)
        except Exception as exc:  # health: allow BLE001 -- unobservable board reads uncached
            log.debug("board rows observer unavailable for %s: %s", path, exc)
            return None
        with _lock:
            obs = _observers.get(path)
            if obs is None or obs.ident != ident or obs.shm != fresh.shm:
                retired = [_drop_observer(path)]
                obs = _observers[path] = fresh
                fresh = None
            else:
                retired = []  # another thread opened a current one first
            retired += _sweep(now)
        _retire(fresh, *retired)
    with _lock:
        obs.used = now
        if _observers.get(path) is obs:
            _observers.move_to_end(path)
    return obs


@board_legacy.reader()
def version(path: str) -> Optional[tuple]:
    """Current commit clock; unobservable boards return None for an uncached read."""
    obs = _observer(str(path))
    return _clock(obs) if obs is not None else None


def _clock(obs: _Observer) -> Optional[tuple]:
    path = obs.path
    try:
        with obs.lock:
            schema = obs.conn.execute("PRAGMA schema_version").fetchone()[0]
            data = obs.conn.execute("PRAGMA data_version").fetchone()[0]
            if obs.conn.in_transaction:  # never expected; a held read snapshot would pin the WAL
                obs.conn.rollback()
                raise sqlite3.OperationalError("observer left a transaction open")
    except Exception as exc:  # health: allow BLE001 -- drop it; the next call opens a new generation
        log.debug("board rows observer failed for %s: %s", path, exc)
        with _lock:
            gone = _drop_observer(path) if _observers.get(path) is obs else None
        _retire(gone)
        return None
    return (obs.gen, *obs.ident, int(schema), int(data))


@board_legacy.reader()
def materialized(key: tuple, path: str, build: Callable[[], T],
                 update: Optional[Callable[[T], T]] = None) -> T:
    """Rows for ``key`` (``key[0]`` must be ``path``), rebuilt when the version moved.
    The version is read before ``build``. A failing build stores nothing, so the next call
    retries; unobservable boards are always built. ``update(previous rows)``, when given,
    replaces ``build`` for a key that already holds older rows: it reads the board in full
    but may reuse what is unchanged (board_read.materialize ``prev``)."""
    ver = version(path)
    if ver is None:
        return build()
    with _lock:
        hit = _entries.get(key)
        if hit is not None and hit[0] == ver:
            _entries.move_to_end(key)
            return hit[1]
    rows = update(hit[1]) if update is not None and hit is not None else build()
    with _lock:
        obs = _observers.get(str(path))
        if obs is not None and obs.gen == ver[0]:  # the observer this version came from is still current
            _entries[key] = (ver, rows)
            _entries.move_to_end(key)
            while len(_entries) > MAX_ENTRIES:
                _entries.popitem(last=False)
    return rows


@board_legacy.reader()
def content_version(path: str) -> Optional[tuple]:
    """Content-only revision for switcher counts, with commit-key fallback on legacy boards."""
    from hermes_cli import kanban_task_change

    ver = version(path)
    if ver is None:
        return None
    with _lock:
        obs = _observers.get(str(path))
    if obs is None:
        return None
    with obs.lock:
        _, cursor = kanban_task_change.changed_since(obs.conn, None)
    return (ver[0], cursor) if cursor is not None else ver


@board_legacy.reader()
def incremental(key: tuple, path: str, build: Callable, fallback: Callable) -> dict:
    """Read cursor, delta and volatile overlay in ONE short-lived guarded snapshot.

    Serialize a board's refreshes through its observer's ``refresh`` lock, so concurrent readers
    cannot publish an older result over a newer cursor. The observer's connection lock is held
    only for the commit-clock PRAGMAs, never across the build: ``version``/``content_version``
    (every GET /boards count poll) borrow it too, and used to queue behind a whole board read
    (15.4 thread-s per minute of lock wait on the hub). An observer dropped mid-build is noticed
    at publish (it is no longer current) and stores nothing. Only the commit-clock observer
    survives; the row reader closes before projection, including on a failed build.
    """
    from hermes_cli import kanban_task_change

    obs = _observer(str(path))
    if obs is None:
        return fallback()
    cache_key = (*key, "p2")
    with obs.refresh:
        with _lock:
            current = _observers.get(str(path)) is obs
            hit = _entries.get(cache_key)
        # Sample only after refresh custody: a waiter must see intervening commits.
        # Failed/retired clocks never certify retained rows.
        commit_key = _clock(obs) if current else None
        if commit_key is None:
            return fallback()
        if hit is not None and len(hit) > 2 and hit[0] is not None and hit[2] == commit_key:
            from hermes_cli import loop_accounting
            with loop_accounting.pass_("kanban.board.rows", skipped=True):
                rows = hit[1]
            cursor = hit[0]
        else:
            conn = _open_reader(path)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("BEGIN")
                changed, cursor = kanban_task_change.changed_since(conn, hit[0] if hit else None)
                rows = build(conn, hit[1] if hit else None, changed)
            finally:
                conn.close()  # even on build/read failure; before Python projection/rules
        with _lock:
            if _observers.get(str(path)) is obs:
                _entries[cache_key] = (cursor, rows, commit_key)
                _entries.move_to_end(cache_key)
                while len(_entries) > MAX_ENTRIES:
                    _entries.popitem(last=False)
        return rows


def retire(paths) -> None:
    """Retire drained authorized paths without disturbing another profile/board."""
    with _lock:
        retired = [_drop_observer(str(path)) for path in paths]
    _retire(*retired)


def clear() -> None:
    """Close every observer and drop all rows (tests, shutdown)."""
    with _lock:
        retired = [_drop_observer(path) for path in list(_observers)]
        _entries.clear()
    _retire(*retired)


def stats() -> dict:
    with _lock:
        return {"observers": len(_observers), "entries": len(_entries)}
