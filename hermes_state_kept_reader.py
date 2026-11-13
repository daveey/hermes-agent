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
    for db in to_close:
        _close(db)
    return len(chosen)


__all__ = ["close_all_under", "read_only", "stats"]
