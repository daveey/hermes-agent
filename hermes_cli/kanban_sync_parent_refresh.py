"""P2 gate for the sync round's foreign-parent mirror. [daveey]

The shadow cursor covers the pull's applied task/run/event rows, not its feed
position (heartbeat/bookkeeping-only pages must not run the mirror). The live
cursor covers local parks and links, including children of a deleted parent.
Unknown coverage runs the original path. Repairs and heals have separate gates;
this gate must never enclose them. A 30s fallback also repairs local mirror-table
clobbers, which intentionally are not task-content changes.
"""
from __future__ import annotations

from collections import OrderedDict
import sqlite3
import threading
import time

from hermes_cli import kanban_task_change as changes
from hermes_cli.loop_accounting import pass_

MAX_AGE_S = 30.0
_LIMIT = 32
_done: OrderedDict[tuple, tuple] = OrderedDict()
_lock = threading.Lock()
_clock = time.monotonic


def reset() -> None:
    with _lock:
        _done.clear()


def refresh_and_wake(live, replica, *, board: str, mode: str, rebuilt: bool) -> None:
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_sync_own_writes import credited

    if mode != "primary":
        return
    with credited(live):
        moved = refresh(live, replica, board=board, rebuilt=rebuilt)
    if moved:
        sync.wake_dispatcher()


def refresh(live, replica, *, board: str, rebuilt: bool = False) -> list[str]:
    from hermes_cli import kanban_db_sync as sync

    # Connection identity prevents a replaced pathname from pairing old-handle
    # rows with the new file's identity; P2 fences normal reopen/restore changes.
    key = (live, replica)
    with _lock:
        previous = _done.pop(key, None)
    began = _clock()
    with pass_(f"sync.parents/{board}") as accounting:
        left = right = None
        local = remote = changes.FULL
        if not live.in_transaction and not replica.in_transaction:
            try:
                local, left = changes.changed_since(live, previous[0] if previous else None)
                remote, right = changes.changed_since(replica, previous[1] if previous else None)
            except (sqlite3.Error, OSError):
                left = right = None
        trusted = left is not None and right is not None
        fallback = not trusted or local is changes.FULL or remote is changes.FULL
        due = previous is None or not 0 <= began - previous[2] < MAX_AGE_S
        skip = trusted and not fallback and not local and not remote and not rebuilt and not due
        # Row-keyed: only rows named by either cursor, plus parents whose evidence-scope membership moved since the
        # scope the previous round read, can differ from the mirror. Wanted membership is re-derived per target
        # (link writes mark both ends; a parent appearing or vanishing locally marks its own id).
        used = previous[3] if previous is not None else None
        if skip:
            accounting.skip()
            result = []
            stamp = previous
        elif (trusted and not fallback and not rebuilt and not due and previous is not None and used is not None
              and isinstance(local, frozenset) and isinstance(remote, frozenset)):
            accounting.change(True)
            scoped = _scope(live)
            # The scope memo returns the same frozenset while its membership holds: no O(scope) difference then.
            moved = frozenset() if scoped is used else scoped ^ used
            result = _refresh_rows(live, replica, local | remote | moved, scoped)
            accounting.rows(len(result))
            # Keep the full refresh's time: the 30s floor (clobber repair) stays anchored to the last full path.
            stamp = (left, right, previous[2], scoped)
        else:
            accounting.fallback(fallback or due or rebuilt)
            accounting.change(bool(local or remote))
            # Build the mirror from exactly the scope it stamps: next round's difference is then against what was applied.
            scoped = _scope(live) if trusted else None
            result = sync.refresh_remote_parents(live, replica, scoped=scoped)
            accounting.rows(len(result))
            # Pre-read cursors: a write during the refresh re-arms the next call.
            stamp = (left, right, began, scoped) if trusted else None
        if stamp is not None:
            with _lock:
                _done[key] = stamp
                while len(_done) > _LIMIT:
                    _done.popitem(last=False)
        return result


def _scope(live) -> frozenset:
    from hermes_cli import kanban_move_parent_evidence as evidence

    return frozenset(evidence._evidence_scope(live))


def _chunks(ids):
    ids = sorted(ids)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        yield part, ",".join("?" * len(part))


def _wanted(live, targets) -> set:
    """The targets ``foreign_parents`` would name: linked as a parent and not a local card."""
    out: set = set()
    for part, marks in _chunks(targets):
        out.update(r[0] for r in live.execute(
            "SELECT DISTINCT l.parent_id FROM task_links l LEFT JOIN tasks p ON p.id = l.parent_id "
            f"WHERE p.id IS NULL AND l.parent_id IN ({marks})", part))
    return out


def _refresh_rows(live, replica, targets, scoped) -> list[str]:
    """``refresh_remote_parents`` restricted to ``targets``: every other parent's rows already match it.

    Same snapshot, values and write-lock rules as the full path, applied per row: a target that is no longer wanted,
    that the replica lost, or (for evidence) that left the scope loses its mirror row. Rows of other parents are left
    as earlier rounds wrote them; the 30s floor rewrites everything, so a local clobber still heals without a pull.
    """
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_move_parent_evidence as evidence
    from hermes_cli.kanban_db_connect import write_txn

    targets = sorted(targets)
    if not targets or not sync.has_remote_parents(live):
        return []
    wanted = _wanted(live, targets)
    replica.execute("SAVEPOINT move_parent_snapshot")
    try:
        found: dict[str, str] = {}
        for part, marks in _chunks(wanted):
            found.update({r[0]: r[1] for r in replica.execute(
                f"SELECT id, status FROM tasks WHERE id IN ({marks})", part)})
        values = {p: (evidence.payload(replica, p) if p in wanted and p in scoped else None) for p in targets}
    finally:
        replica.execute("RELEASE move_parent_snapshot")
    current: dict[str, str] = {}
    for part, marks in _chunks(targets):
        current.update({r[0]: r[1] for r in live.execute(
            f"SELECT parent_id, status FROM {sync.REMOTE_PARENTS_TABLE} WHERE parent_id IN ({marks})", part)})
    stored = evidence.stored(live, targets)
    changed = [p for p in targets if current.get(p) != found.get(p)]
    evidence_changed = {p: values[p] for p in targets if stored.get(p) != values[p]}
    if not changed and not evidence_changed:
        return []  # An unchanged sync round must not acquire a primary write lock.
    with write_txn(live):
        if evidence_changed:
            evidence.apply_rows(live, evidence_changed)
        if changed:
            now = int(time.time())
            live.executemany(f"DELETE FROM {sync.REMOTE_PARENTS_TABLE} WHERE parent_id = ?", [(p,) for p in changed])
            live.executemany(
                f"INSERT INTO {sync.REMOTE_PARENTS_TABLE} (parent_id, status, refreshed_at) VALUES (?, ?, ?)",
                [(p, found[p], now) for p in changed if p in found])
    return sorted(set(changed) | set(evidence_changed))
