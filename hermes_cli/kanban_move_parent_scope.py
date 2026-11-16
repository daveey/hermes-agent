"""Incremental parked and queued evidence scopes (card t_01a1d59e565b0b7c). [daveey]

The scope is the union, over active children of a missing (foreign) parent whose latest started run ended ``blocked``,
of the parents named by that run's authenticated review checkpoint. Children can share parents, so the memo keeps each
child's own contribution (``child -> frozenset(parents)``) plus a per-parent reference count; the union is rebuilt only
when a count crosses zero, so a keyed round costs O(changed), not O(scope).

``kanban_task_change.changed_since`` (P2) names the tasks written since the memoized cursor. The affected children are
those tasks themselves plus every child linked from them (a parent that appears, vanishes or changes flips its
children's foreign-parent membership; link writes mark both ends). Only those are recomputed.
Queued contributions include all parents of active tasks with a checkpointed latest moved event;
only changed tasks are refreshed, without the parked scope's child expansion.

Fail-closed rules:
- a connection already inside a transaction bypasses the memo (its uncommitted rows may roll back and must not poison it);
- ``FULL``/no cursor/in-memory database recompute everything and re-arm from the fresh cursor (or drop the memo);
- the memo stores (cursor, parked, queued) atomically from one snapshot, bounded to ``_MAX`` paths; file
  identity, ``-shm`` and schema fences live in the P2 cursor.
"""
from __future__ import annotations

import sqlite3
import threading

from hermes_cli import kanban_task_change as tc

_MAX = 32
_CHUNK = 400
_LOCK = threading.Lock()
_MEMO: dict[tuple, tuple] = {}  # (connection, main path) -> (Cursor, parked map, queued map)

_MEMBER = ("SELECT DISTINCT t.id FROM tasks t JOIN task_links l ON l.child_id = t.id "
           "LEFT JOIN tasks p ON p.id = l.parent_id "
           "WHERE p.id IS NULL AND t.status NOT IN ('done', 'archived')")


def _contribution(conn, task_id) -> frozenset:
    from hermes_cli.kanban_move_parent_evidence import _parked_review_checkpoint
    from hermes_cli.kanban_unused_grant import latest_started_run

    run = latest_started_run(conn, task_id)
    if run is None or run["outcome"] != "blocked":
        return frozenset()
    evidence = _parked_review_checkpoint(conn, task_id, run)
    return frozenset(evidence["parents"]) if evidence else frozenset()


def _full(conn) -> dict:
    out = {}
    for (task_id,) in conn.execute(_MEMBER).fetchall():
        parents = _contribution(conn, task_id)
        if parents:
            out[task_id] = parents
    return out


def _chunks(ids):
    ids = sorted(ids)
    for i in range(0, len(ids), _CHUNK):
        yield ids[i:i + _CHUNK]


class _Scope:
    """One scope's per-task contributions plus a reference count per parent, so a keyed round costs O(changed),
    not O(scope): the union is rebuilt only when a parent's count crosses zero. Mutated in place only while the
    memo entry is checked out (popped); an exception drops the entry, so the next call recomputes in full."""

    __slots__ = ("by_task", "counts", "union")

    def __init__(self, by_task: dict) -> None:
        self.by_task = by_task
        self.counts: dict = {}
        for parents in by_task.values():
            for p in parents:
                self.counts[p] = self.counts.get(p, 0) + 1
        self.union = frozenset(self.counts)

    def set(self, updates: dict) -> bool:
        """``updates`` maps task -> its new contribution (empty drops it). True when the union's membership moved."""
        touched = set()
        for task_id, parents in updates.items():
            for p in self.by_task.pop(task_id, ()):
                n = self.counts[p] - 1
                if n:
                    self.counts[p] = n
                else:
                    del self.counts[p]
                    touched.add(p)
            if parents:
                self.by_task[task_id] = parents
                for p in parents:
                    n = self.counts.get(p, 0)
                    if not n:
                        touched.add(p)
                    self.counts[p] = n + 1
        if any((p in self.counts) != (p in self.union) for p in touched):
            self.union = frozenset(self.counts)
            return True
        return False


def _refresh(conn, changed) -> dict:
    """New contributions of every task the change can affect (empty frozenset: no longer a member)."""
    affected = set(changed)
    for part in _chunks(changed):
        marks = ",".join("?" * len(part))
        affected.update(r[0] for r in conn.execute(
            f"SELECT child_id FROM task_links WHERE parent_id IN ({marks})", part))
    updates = dict.fromkeys(affected, frozenset())
    for part in _chunks(affected):
        marks = ",".join("?" * len(part))
        for (task_id,) in conn.execute(f"{_MEMBER} AND t.id IN ({marks})", part).fetchall():
            updates[task_id] = _contribution(conn, task_id)
    return updates


def _union(contributions: dict) -> frozenset:
    out: set = set()
    for parents in contributions.values():
        out.update(parents)
    return frozenset(out)


def _queued(conn, changed=None) -> dict:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_move_parent_evidence import _QUEUED, _queued_checkpoint

    out = {}
    parts = [None] if changed is None else _chunks(changed)
    for part in parts:
        query = _QUEUED if part is None else f"{_QUEUED} AND t.id IN ({','.join('?' * len(part))})"
        for event in conn.execute(query, () if part is None else part).fetchall():
            if _queued_checkpoint(event["payload"]):
                parents = frozenset(kb.parent_ids(conn, event["task_id"]))
                if parents:
                    out[event["task_id"]] = parents
    return out


def parked_scope(conn) -> frozenset:
    return _scope(conn, include_queued=False)


def evidence_scope(conn) -> frozenset:
    return _scope(conn, include_queued=True)


def _scope(conn, *, include_queued) -> frozenset:
    path = tc._main_path(conn)
    if path is None or conn.in_transaction:
        return _union(_full(conn)) | (_union(_queued(conn)) if include_queued else frozenset())
    key = (conn, path)  # do not reuse a pathname stamp collected through an older open handle
    with _LOCK:
        held = _MEMO.pop(key, None)  # checked out: mutated in place below, put back only on success
    conn.execute("BEGIN")  # one snapshot for the cursor and every row read below
    try:
        try:
            changed, cursor = tc.changed_since(conn, held[0] if held else None)
        except sqlite3.Error:
            changed, cursor = tc.FULL, None
        if changed is tc.FULL or cursor is None or held is None:
            parked, queued = _Scope(_full(conn)), _Scope(_queued(conn))
            both = parked.union | queued.union
        else:
            _, parked, queued, both = held
            moved = parked.set(_refresh(conn, changed)) if changed else False
            if changed:
                fresh = _queued(conn, changed)
                moved = queued.set({k: fresh.get(k, frozenset()) for k in changed}) or moved
            if moved:
                both = parked.union | queued.union
    finally:
        conn.execute("COMMIT")
    if cursor is not None:
        with _LOCK:
            while key not in _MEMO and len(_MEMO) >= _MAX:
                _MEMO.pop(next(iter(_MEMO)))
            _MEMO[key] = (cursor, parked, queued, both)
    return both if include_queued else parked.union


def _reset_for_tests() -> None:
    with _LOCK:
        _MEMO.clear()
