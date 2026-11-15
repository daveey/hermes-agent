"""Per-task incremental memo for dispatcher structural scans (P2 consumer, G2c t_01a1db079d8d4505). [daveey]

``kanban_sweep_gate.memo`` reuses a whole-board scan while the board's content revision holds. On the hub's
``default`` board that revision moves ~16 times a minute, so such a memo misses on nearly every dispatcher tick and
each structural scan (``foreign_ids``: every ``tasks`` row; ``candidate_task_ids``: a status/event join) ran in full,
several times a tick. This memo keeps a per-task answer instead and refreshes only the tasks the per-task change
cursor (``kanban_task_change.changed_since``) names.

State per (file identity, name, extra key) is one immutable ``(Cursor, mapping task_id -> value)`` pair:

* each call opens one snapshot (``BEGIN``), asks ``changed_since`` which tasks moved after the stored cursor, and
  then reads in that same snapshot: ``full(conn)`` on ``FULL``, else ``partial(conn, ids)``, merged over the stored
  mapping (an id ``partial`` does not return is removed: it was deleted or no longer qualifies). ``COMMIT``; the new
  cursor describes exactly the rows read;
* the pair is swapped in under a lock only when it is newer than the stored one (compare-and-swap on the
  revision), so a slower thread never moves it backwards;
* inside a caller's transaction the memo reads through (the cursor then also covers the caller's own uncommitted
  writes, which ``partial`` sees) but never stores: the caller may roll back and the revision it bumped to can be
  reused by other rows (as ``kanban_sweep_gate.memo``);
* nothing is stored without a cursor (in-memory file, untrusted coverage, a file swapped while reading);
* reuse/publication requires the managed connection factory's open-time inode attestation. Raw connections,
  newly created files (absent before open), and retained handles to replaced files read through without caching;
  a pathname stat at first memo use cannot establish which inode SQLite actually opened;
* only tasks whose value is truthy are stored, and memory is bounded (``MAX_SLOTS`` slots, ``MAX_ENTRIES`` per slot).

Values must be functions of the task's OWN rows only (its ``tasks`` row and rows keyed by its ``task_id`` in the
watched tables): a change to another task marks only that task, so a value embedding another task's row would go
stale. A consumer declares the columns it reads (``columns``); the cursor is trusted only while each table's update
trigger watches each of them (a column added after the triggers were installed is not watched until a writer init
reinstalls them), and a bookkeeping column (``kanban_activity_revision.BOOKKEEPING_COLUMNS``) is refused. It also
declares the event kinds it reads (``event_kinds``); a kind in ``kanban_activity_revision.UNREAD_EVENT_KINDS``
(heartbeats) is refused, because its inserts never mark a task. Tables outside the watched set
(``task_wake_checks``, ``kanban_sync_state``) are never memoized here.
"""
from __future__ import annotations

import sqlite3
import threading
from types import MappingProxyType
from typing import Any, Callable, Hashable, Iterable, Mapping, Optional

from hermes_cli import kanban_activity_revision as _rev
from hermes_cli import kanban_task_change as _tc

MAX_SLOTS = 256
MAX_ENTRIES = 200_000
CHUNK = 500  # ids per ``partial`` call (well under SQLite's bound-parameter limit)

_EMPTY: Mapping[str, Any] = MappingProxyType({})


class _State:
    """An immutable (cursor, mapping) pair, plus the derived views consumers ask for (built once per pair)."""

    __slots__ = ("_views", "cursor", "values")

    def __init__(self, cursor: Optional[_tc.Cursor], values: Mapping[str, Any]):
        self.cursor = cursor
        self.values = values
        self._views: dict = {}

    def view(self, name: str, build: Callable[[Mapping[str, Any]], Any]) -> Any:
        hit = self._views.get(name)
        if hit is None:
            hit = self._views[name] = build(self.values)
        return hit


_STATES: dict[tuple, _State] = {}
_LOCK = threading.Lock()


def _check_kinds(event_kinds: Iterable[str]) -> None:
    unread = set(event_kinds) & set(_rev.UNREAD_EVENT_KINDS)
    if unread:
        raise ValueError(f"kanban_task_memo: event kinds {sorted(unread)} never mark their task (heartbeats do not "
                         "bump the activity revision), so a per-task memo of them would go stale")


def _check_columns(watched: tuple) -> None:
    for table, cols in watched:
        unwatched = set(cols) & _rev.BOOKKEEPING_COLUMNS.get(table, frozenset())
        if unwatched:
            raise ValueError(f"kanban_task_memo: {table} columns {sorted(unwatched)} are bookkeeping (their updates "
                             "never mark a task), so a per-task memo of them would go stale")


# (path, dev, ino, schema_version, watched) -> whether every declared column is in its table's update trigger. A
# column added after the triggers were installed (``ALTER TABLE ... ADD COLUMN``: a sync migration on an open
# connection) is not watched until the next writer init reinstalls them; ``schema_version`` moves with either.
_WATCHES: dict[tuple, bool] = {}


def _watches(conn: sqlite3.Connection, ident: tuple, schema: int, watched: tuple) -> bool:
    key = (*ident, schema, watched)
    hit = _WATCHES.get(key)
    if hit is not None:
        return hit
    ok = True
    for table, cols in watched:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                           (f"{_rev.PREFIX}{table}_update",)).fetchone()
        sql = row[0] if row else ""
        if any(f"OLD.{_rev._quote(col)} IS NOT NEW.{_rev._quote(col)}" not in sql for col in cols):
            ok = False
            break
    with _LOCK:
        if len(_WATCHES) >= MAX_SLOTS:
            _WATCHES.clear()
        _WATCHES[key] = ok
    return ok


def _store(slot: tuple, state: _State) -> None:
    """Compare-and-swap: keep the stored pair unless ``state`` is newer (same file and schema, higher revision) or
    the stored pair describes another file generation (whose cursor could never be trusted again anyway)."""
    new = state.cursor
    if new is None:  # never store without a cursor
        return
    with _LOCK:
        held = _STATES.get(slot)
        if held is not None and held.cursor is not None:
            old = held.cursor
            same = (old.dev, old.ino, old.shm, old.schema_version, old.tables) == (
                new.dev, new.ino, new.shm, new.schema_version, new.tables)
            if same and new.revision <= old.revision:
                return
        if held is None and len(_STATES) >= MAX_SLOTS:
            _STATES.clear()
        _STATES[slot] = state


def _partial(conn: sqlite3.Connection, partial: Callable, ids: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i in range(0, len(ids), CHUNK):
        out.update(partial(conn, ids[i:i + CHUNK]))
    return out


def state(name: str, conn: sqlite3.Connection, *,
          full: Callable[[sqlite3.Connection], Mapping[str, Any]],
          partial: Callable[[sqlite3.Connection, list[str]], Mapping[str, Any]],
          columns: Mapping[str, Iterable[str]], extra: Hashable = None,
          event_kinds: Iterable[str] = ()) -> _State:
    """The memo's current pair for ``name`` (see the module docstring). ``full(conn)`` returns ``task_id -> value``
    for every task; ``partial(conn, ids)`` returns it for the tasks among ``ids`` that still exist. Falsy values are
    dropped. ``columns`` names every column the value reads, per watched table: the cursor is trusted only while each
    table's update trigger watches each of them. Raises ``ValueError`` for a heartbeat-kind or bookkeeping-column
    consumer and ``sqlite3.Error`` like any read."""
    _check_kinds(event_kinds)
    watched = tuple(sorted((table, tuple(sorted(set(cols)))) for table, cols in columns.items()))
    _check_columns(watched)
    tables = tuple(table for table, _cols in watched)
    from hermes_cli.kanban_sweep_gate import file_identity
    ident = file_identity(conn)
    if ident is None or getattr(conn, "_hermes_open_identity", None) != ident[1:]:
        # Raw connections (and files created by this open) have no open-time
        # attestation. A pathname stat cannot identify the inode they read.
        return _State(None, _clean(full(conn)))
    slot = (ident, name, extra)
    held = _STATES.get(slot)
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")  # one snapshot: the cursor describes exactly the rows read below
    try:
        try:
            changed, fresh = _tc.changed_since(conn, held.cursor if held is not None else None, tables=tables)
            if fresh is not None and not _watches(conn, ident, fresh.schema_version, watched):
                changed, fresh = _tc.FULL, None  # a column added after the triggers: its updates mark nothing
        except sqlite3.Error:
            changed, fresh = _tc.FULL, None  # a failed cursor read only costs the full read; nothing is stored
        if changed is _tc.FULL or held is None:
            values: Mapping[str, Any] = _clean(full(conn))
        elif not changed:
            values = held.values
        else:
            ids = sorted(changed)  # type: ignore[arg-type]  # a frozenset here: FULL took the branch above
            got = _partial(conn, partial, ids)
            old = held.values
            moved = [tid for tid in ids if (got.get(tid) or None) != (old.get(tid) or None)]
            if not moved:
                values = old  # e.g. a comment on a task whose value did not change: keep the derived views
            else:
                merged = dict(old)
                for tid in moved:
                    value = got.get(tid)
                    if value:
                        merged[tid] = value
                    else:
                        merged.pop(tid, None)
                values = MappingProxyType(merged)
    finally:
        if own:
            conn.execute("COMMIT")
    if file_identity(conn) != ident or (fresh is not None and (fresh.dev, fresh.ino) != ident[1:]):
        fresh = None  # replacement during cursor/full/partial reads: never publish under either file
    if held is not None and values is held.values:
        if fresh == held.cursor:
            return held
        result = _State(fresh, values)
        result._views = held._views  # same values: the derived views still hold (read-only after build)
    else:
        result = _State(fresh, values)
    if own and fresh is not None and result is not held and len(values) <= MAX_ENTRIES:
        _store(slot, result)
    return result


def _clean(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({k: v for k, v in values.items() if v})


def reset() -> None:
    """Forget every memo (tests)."""
    with _LOCK:
        _STATES.clear()
        _WATCHES.clear()
