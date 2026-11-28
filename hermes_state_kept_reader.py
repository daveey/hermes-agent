"""Kept read-only ``state.db`` handles for background readers. [daveey]

A background pass that checks a chat before acting (``tui_gateway.kanban_remote_notices._session_db``: does the
session exist, its compression tip, archived/moved, a worker transcript, a gateway lease, the notice already in the
transcript) opened a WRITABLE ``SessionDB`` per link per pass. Each open ran the writer preflight, schema reconcile
(``_init_schema``) and the lock-guard fd scan, and its close a WAL checkpoint: the hub gateway's
``hermes_state.__init__`` writer opens under ``_deliver_link`` (change-proportional design, card I).

:func:`read_only` keeps ONE ``SessionDB(read_only=True)`` per resolved path for the process: no schema reconcile
at all on the read path, and the read-only open's own probes (FTS tables) run once per kept handle, not per pass.

A kept handle is handed out only while it still reads the live file. Before each borrow it is retired (and a fresh
one opened) when:

- the path's ``(st_dev, st_ino)`` changed (a restore, a recovery swap, a moved/deleted profile store);
- its ``-wal``/``-shm`` identity changed (a new WAL generation, so the handle may be stranded on a deleted one);
- the handle was closed (test teardown, a profile delete's ``close_all_under``) or quarantined;
- a body raised ``sqlite3.DatabaseError`` on it.

A retired handle stays open for borrowers already holding it and is closed by the last of them. Writers keep using
``hermes_state_registry.acquire`` (shared writer, schema reconcile per generation, not per call).
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional

from hermes_state_common import path_is_under as _path_is_under
from hermes_state_common import stat_db_file_identity as _stat_db_file_identity

if TYPE_CHECKING:  # pragma: no cover
    from hermes_state import SessionDB


class _Kept:
    __slots__ = ("db", "identity", "path", "refs", "retired", "sidecars")

    def __init__(self, path: Path, db: SessionDB, identity, sidecars) -> None:
        self.path = path
        self.db = db
        self.identity = identity
        self.sidecars = sidecars
        self.refs = 0
        self.retired = False


_lock = threading.Lock()  # map and refcount operations only; opens and closes run outside it
_kept: dict[Path, _Kept] = {}

# Observability for tests and P1 readback.
stats = {"opens": 0, "reuses": 0, "reopens": 0}


def _sidecars(path: Path) -> dict:
    from hermes_state_dbfile import _stat_sqlite_sidecar_identity

    return _stat_sqlite_sidecar_identity(path)


def _open_session_db(path: Path) -> SessionDB:
    """Call-time import avoids cycles; tests patch this."""
    from hermes_state import SessionDB

    return SessionDB(path, read_only=True)


def _usable(kept: _Kept) -> bool:
    db = kept.db
    if kept.retired or getattr(db, "_conn", None) is None or getattr(db, "_db_corrupt", False):
        return False
    if _stat_db_file_identity(kept.path) != kept.identity:
        return False
    return _sidecars(kept.path) == kept.sidecars


def _close(db: SessionDB) -> None:
    with contextlib.suppress(Exception):
        db.close()


def _retire(kept: _Kept) -> bool:
    """Take *kept* out of the map (caller holds ``_lock``); True when nobody borrows it, so the caller closes it."""
    kept.retired = True
    if _kept.get(kept.path) is kept:
        del _kept[kept.path]
    return kept.refs <= 0


def _key(db_path) -> Path:
    try:
        return Path(os.path.realpath(os.fspath(db_path)))
    except (OSError, TypeError, ValueError):
        return Path(db_path)


def _borrow(path: Path) -> _Kept:
    with _lock:
        kept = _kept.get(path)
        if kept is not None:
            kept.refs += 1
    if kept is not None:
        if _usable(kept):
            stats["reuses"] += 1
            return kept
        with _lock:
            kept.refs -= 1
            close_now = _retire(kept)
        if close_now:
            _close(kept.db)
        stats["reopens"] += 1
    identity = _stat_db_file_identity(path)
    db = _open_session_db(path)
    # Sidecars after the open: a WAL reader's open itself may create the -shm/-wal it then reads through.
    sidecars = _sidecars(path)
    if _stat_db_file_identity(path) != identity:
        identity = None  # replaced during the open: serve this borrow, never match on the next one
    fresh = _Kept(path, db, identity, sidecars)
    fresh.refs = 1
    stats["opens"] += 1
    with _lock:
        current = _kept.get(path)
        if current is None or current.retired:
            _kept[path] = fresh
            return fresh
        # Another thread installed one while this one opened: keep theirs, drop ours.
        current.refs += 1
    _close(db)
    return current


def _give_back(kept: _Kept, *, broken: bool = False) -> None:
    with _lock:
        kept.refs -= 1
        close_now = _retire(kept) if broken else (kept.retired and kept.refs <= 0)
    if close_now:
        _close(kept.db)


@contextlib.contextmanager
def read_only(db_path) -> Iterator[SessionDB]:
    """The process's kept ``SessionDB(read_only=True)`` for *db_path*, borrowed for the ``with`` body. The file
    must exist (``SessionDB(read_only=True)`` raises otherwise). Never write through it."""
    kept = _borrow(_key(db_path))
    broken = False
    try:
        yield kept.db
    except sqlite3.DatabaseError:
        broken = True
        raise
    finally:
        _give_back(kept, broken=broken)


def close_all_under(directory: Optional[str | Path] = None) -> int:
    """Retire every kept handle (under *directory* when given); idle ones close now, borrowed ones at their last
    give-back. ``hermes_state_registry.close_all``/``close_all_under`` call this, so a profile delete's rmtree never
    meets a kept descriptor. Returns how many were retired."""
    root = None if directory is None else _key(Path(directory).expanduser())
    with _lock:
        chosen = [k for p, k in _kept.items() if root is None or _path_is_under(p, root)]
        to_close = [k.db for k in chosen if _retire(k)]
        lent_paths = [p for p in _lend_epoch if root is None or _path_is_under(p, root)]
        for path in lent_paths:
            # Lent handles out right now carry the old epoch and close at their give-back.
            _lend_epoch[path] = _lend_epoch.get(path, 0) + 1
            to_close.extend(lent.db for lent in _idle.pop(path, ()))
    for db in to_close:
        _close(db)
    return len(chosen)


# ── Exclusive lending (request readers) ──────────────────────────────────────────────────────────────────────────
# [daveey] ``read_only`` above shares ONE handle among concurrent borrowers, which serialize on its connection lock:
# right for a background pass, wrong for request handlers, where one long read (an FTS search, an export) would
# stall every other request on that store. ``lend`` hands each borrower its own idle handle (opening one on a miss)
# and takes it back afterwards, so a request no longer pays a fresh connection's schema parse (49 triggers, ~40 KB
# of trigger SQL: 1.3-1.4 ms per open on the hub) for each ``GET /api/sessions/{id}/messages``.
#
# An idle handle is lent again only while a fresh open would see exactly what it sees: the same ``state.db`` file
# identity, the same ``-wal``/``-shm`` identities, the same ``PRAGMA schema_version`` as when it was opened (so the
# open-time FTS flags and the caller's schema probe still hold), no transaction left open, not closed, not
# quarantined, and its profile not retired. Reads are autocommit, so each one starts a new snapshot and sees every
# commit made before it: reuse is keyed on what the handle reads, never on a clock. Idle handles hold their profile
# lease like any open SessionDB, and the profile retirement monitor closes them like any other owner, so a profile
# delete is never blocked by a pooled handle.

_LEND_IDLE_PER_PATH = 2
_idle: dict[Path, list] = {}
_lend_epoch: dict[Path, int] = {}
lend_stats = {"opens": 0, "reuses": 0, "discards": 0}


class _Lent:
    __slots__ = ("db", "epoch", "identity", "path", "schema", "sidecars")

    def __init__(self, path: Path, db, identity, sidecars, schema, epoch: int) -> None:
        self.path = path
        self.db = db
        self.identity = identity
        self.sidecars = sidecars
        self.schema = schema
        self.epoch = epoch


def _schema_cookie(db) -> Optional[int]:
    conn = getattr(db, "_conn", None)
    if conn is None:
        return None
    try:  # the caller holds the handle exclusively (a fresh open, or one taken out of the idle pool)
        return conn.execute("PRAGMA schema_version").fetchone()[0]
    except (sqlite3.Error, AttributeError, TypeError):
        return None


def _lendable(lent: _Lent) -> bool:
    """True while *lent* reads exactly what a fresh open of its path would."""
    db = lent.db
    conn = getattr(db, "_conn", None)
    if conn is None or getattr(db, "_db_corrupt", False) or lent.identity is None:
        return False
    try:
        if conn.in_transaction:
            return False
    except sqlite3.Error:
        return False
    lease = db.__dict__.get("_profile_lease")
    try:
        if lease is not None and lease.retired():
            return False
    except OSError:
        return False
    if _stat_db_file_identity(lent.path) != lent.identity or _sidecars(lent.path) != lent.sidecars:
        return False
    return lent.schema is not None and _schema_cookie(db) == lent.schema


def _take_idle(path: Path) -> Optional[_Lent]:
    while True:
        with _lock:
            pool = _idle.get(path)
            if not pool:
                return None
            lent = pool.pop()
            if not pool:
                del _idle[path]
            current = lent.epoch == _lend_epoch.get(path, 0)
        if current and _lendable(lent):
            lend_stats["reuses"] += 1
            return lent
        lend_stats["discards"] += 1
        _close(lent.db)


def _put_back(lent: _Lent, *, broken: bool) -> None:
    keep = not broken and _lendable(lent)
    if keep:
        with _lock:
            if lent.epoch == _lend_epoch.get(lent.path, 0):
                pool = _idle.setdefault(lent.path, [])
                if len(pool) < _LEND_IDLE_PER_PATH:
                    pool.append(lent)
                    return
    lend_stats["discards"] += 1
    _close(lent.db)


@contextlib.contextmanager
def lend(db_path, opener) -> Iterator[SessionDB]:
    """An exclusive read-only ``SessionDB`` for *db_path* for the ``with`` body: an idle lendable one, else
    ``opener()`` (which must return a fresh ``SessionDB(read_only=True)`` for that path; its errors propagate and
    nothing is pooled). Never write through it and never ``close()`` it: the give-back decides."""
    path = _key(db_path)
    lent = _take_idle(path)
    if lent is None:
        with _lock:
            epoch = _lend_epoch.setdefault(path, 0)  # every lent path is known to ``close_all_under``
        identity = _stat_db_file_identity(path)
        db = opener()
        try:
            sidecars = _sidecars(path)
            schema = _schema_cookie(db)
            if _stat_db_file_identity(path) != identity:
                identity = None  # replaced during the open: serve this borrow, never lend it again
        except BaseException:
            _close(db)
            raise
        lent = _Lent(path, db, identity, sidecars, schema, epoch)
        lend_stats["opens"] += 1
    broken = False
    try:
        yield lent.db
    except sqlite3.DatabaseError:
        broken = True
        raise
    finally:  # any other caller error says nothing about the handle; ``_lendable`` still checks it
        _put_back(lent, broken=broken)


__all__ = ["close_all_under", "lend", "lend_stats", "read_only", "stats"]
