"""Run a dispatcher sub-sweep only when its input moved or a due time derived from its data has arrived. [daveey]

G2 of the change-proportional design (t_01a1d563324d7e8e §3, review R13, condition C12). Every ``dispatch_once``
tick used to run each reclaim/maintenance sub-sweep in full, although on a quiet board none of their inputs moved.

A gated sweep runs when any of these holds:

* its **key** is unknown (no activity counter in the file, the caller holds a transaction, a read failed);
* its key differs from the key read *before* its last completed run. The key is the file identity, the
  ``kanban_activity_revision`` counter of the task tables (claim, lease and heartbeat writes do not move it) and
  the sweep's own ``extra`` read of any other table it reads;
* its **due time** has arrived. ``due(conn)`` is re-read on every tick from the rows (``MIN(claim_expires)``, a
  blocker's next probe, a park's wake), never memoized: time and process liveness are decisions, not structure.
  It returns the earliest epoch second at which the sweep's own time policy could act on unchanged rows, or None
  when nothing in the rows can come due.

The key is read before the sweep runs, so a write that lands during the run moves the next tick's key. A sweep's
own writes move the key too, which costs one extra run and then settles.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Hashable, Optional

from hermes_cli import kanban_activity_revision as _rev
from hermes_cli import loop_accounting as _la

_GATES: dict[tuple, tuple] = {}  # (file identity, sweep name) -> (key read before its last run, run due time)
_MEMOS: dict[tuple, tuple] = {}  # (file identity, memo name) -> (key, value)
_LOCK = threading.Lock()
_MAX = 1024


def file_identity(conn: sqlite3.Connection) -> Optional[tuple]:
    """``(path, device, inode)`` of ``conn``'s main file, or None (in-memory, unreadable)."""
    try:
        path = next((f for _s, n, f in conn.execute("PRAGMA database_list") if n == "main"), None)
    except sqlite3.Error:
        return None
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (path, st.st_dev, st.st_ino)


def content_revision(conn: sqlite3.Connection, ident: Optional[tuple] = None) -> Optional[tuple]:
    """``(revision, schema_version)`` of the board's task tables, or None when the counter cannot vouch for them."""
    ident = ident or file_identity(conn)
    if ident is None:
        return None
    if conn.in_transaction:
        # Inside the caller's transaction the counter is read in the same snapshot as the rows, and the caller's
        # own uncommitted writes have bumped it through the triggers, so it still vouches for what the caller
        # sees. Only the coverage proof (a sqlite_master scan that needs its own BEGIN) must already be known.
        with _rev._COVERED_LOCK:
            version = _rev._COVERED.get((ident[0], ident[1], ident[2], _rev.LOCAL_TABLES))
        if version is None:
            return None
        try:
            row = conn.execute(_rev._READ_SQL).fetchone()
        except sqlite3.Error:
            return None
        return (int(row[0]), int(row[1])) if row is not None and int(row[1]) == version else None
    try:
        return _rev._covered_read(conn, Path(ident[0]), (ident[1], ident[2]), _rev.LOCAL_TABLES)
    except sqlite3.Error:
        return None


def _put(table: dict, slot: tuple, value) -> None:
    with _LOCK:
        if len(table) >= _MAX:
            table.clear()
        table[slot] = value


def key(conn: sqlite3.Connection, extra: Optional[Callable[[sqlite3.Connection], Hashable]] = None,
        ident: Optional[tuple] = None) -> Optional[tuple]:
    """The gate key for one sweep (see the module docstring), or None when unknown."""
    ident = ident or file_identity(conn)
    revision = content_revision(conn, ident)
    if revision is None:
        return None
    try:
        from hermes_cli import kanban_home_revision as custody
        home_revision = custody.read(conn)
        if home_revision is None:
            return None
        return (ident, revision, home_revision, extra(conn) if extra is not None else None)
    except sqlite3.Error:
        return None


def is_due(conn: sqlite3.Connection, due: Optional[Callable[[sqlite3.Connection], Optional[float]]],
           now: float) -> bool:
    if due is None:
        return False
    try:
        at = due(conn)
    except sqlite3.Error:
        return True  # an unreadable due time is unknown: run
    return at is not None and float(at) <= now


def gated(name: str, conn: sqlite3.Connection, fn: Callable, /, *args: Any,
          extra: Optional[Callable[[sqlite3.Connection], Hashable]] = None,
          due: Optional[Callable[[sqlite3.Connection], Optional[float]]] = None,
          run_due: Optional[Callable[[Any], Optional[float]]] = None,
          skipped: Any = None, now: Optional[float] = None, **kwargs: Any) -> Any:
    """Call ``fn(*args, **kwargs)`` as the accounted pass ``name`` unless its key is unchanged and no due time has
    arrived; then return ``skipped`` and count a skip.

    ``due(conn)`` is read on every tick (for due times kept in bookkeeping columns or tables outside the key).
    ``run_due(result)`` is the earliest due time the run itself derived from the rows it read (a park's wake, a
    probe interval's end); it is kept with the key, which is sound because those rows cannot change while the key
    holds."""
    ident = file_identity(conn)
    k = key(conn, extra, ident)
    storable = k is not None and not conn.in_transaction  # see ``memo``: a rolled-back bump can recur
    slot = (ident, name)
    t = time.time() if now is None else now
    held = _GATES.get(slot)
    if (k is not None and held is not None and held[0] == k and (held[1] is None or t < held[1])
            and not is_due(conn, due, t)):
        with _la.pass_(name, skipped=True):
            return skipped
    with _LOCK:
        _GATES.pop(slot, None)  # a failed run must not leave the old key vouching for it
    result = _la.timed(name, fn, *args, **kwargs)
    if storable:
        try:
            at = run_due(result) if run_due is not None else None
        except Exception:  # health: allow BLE001 -- an unknown due time only means the next tick runs
            return result
        _put(_GATES, slot, (k, None if at is None else float(at)))
    return result


def memo(name: str, conn: sqlite3.Connection, compute: Callable[[sqlite3.Connection], Any],
         extra: Optional[Callable[[sqlite3.Connection], Hashable]] = None) -> Any:
    """``compute(conn)``, reused while the gate key holds. Only for structural values (no clock, no liveness);
    the caller must not mutate the returned value."""
    ident = file_identity(conn)
    k = key(conn, extra, ident)
    if k is None:
        return compute(conn)
    slot = (ident, name)
    hit = _MEMOS.get(slot)
    if hit is not None and hit[0] == k:
        return hit[1]
    value = compute(conn)
    if not conn.in_transaction:  # a key read in a transaction that later rolls back could recur on other rows
        _put(_MEMOS, slot, (k, value))
    return value


def config_key(_conn: Optional[sqlite3.Connection] = None) -> Hashable:
    """The config file's cache signature: sweeps whose policy reads ``kanban.*`` config rerun when it changes."""
    try:
        from hermes_cli.config import get_config_path
        from hermes_cli.config_backend import config_cache_version_or_none
        return config_cache_version_or_none(get_config_path())
    except Exception:  # health: allow BLE001 -- an unreadable signature is a fresh key, so the sweep runs
        return object()


def reset() -> None:
    """Forget every gate and memo (tests, and a board file replaced in place)."""
    with _LOCK:
        _GATES.clear()
        _MEMOS.clear()
