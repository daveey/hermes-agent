"""Card-id lookups in this host's Board replicas (``kanban/sync-shadow/<board>.db``). [daveey]

A Board-synced host's live board file holds only part of each board (its own cards plus the copies it claimed); its
Board replica holds the whole board. So an id the live file lacks may still be a real card, and the phantom-ref
checks (the ``created_cards`` completion gate and the summary scan/diagnostics) ask the replica before the hub
replica, which can answer "fresh" yet lack a card minted minutes earlier.

Lookups borrow the kept read-only readers (``kanban_session_activity.cached_ro``): the board diagnostics re-resolve
an active phantom-ref warning on every ``GET /board``, and opening each replica afresh (twice, plus hub.db) cost
~55 ms of a ~130 ms GET on hermes-hub (card E, t_01a1d59e5543daa1). An idle autocommit reader holds no snapshot, so
every lookup still sees the latest commit.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Iterable, Optional, TypeVar

T = TypeVar("T")


def _replica_paths() -> list[Path]:
    from hermes_cli.kanban_db import kanban_home
    from hermes_cli.kanban_db_sync import SHADOW_DIR

    try:
        return sorted(p for p in (kanban_home() / "kanban" / SHADOW_DIR).glob("*.db") if p.is_file())
    except OSError:
        return []


def with_reader(path: Path, body: Callable[[sqlite3.Connection], T]) -> T:
    """``body(conn)`` on the kept read-only reader of ``path``. A reader evicted while this call waited for it is
    retried once on a fresh one (a one-shot open never saw that race); any other sqlite error propagates."""
    from hermes_cli.kanban_generation import DeletedWalGenerationError
    from hermes_cli.kanban_session_activity import cached_ro

    for attempt in (0, 1):
        try:
            with cached_ro(path) as conn:
                return body(conn)
        except DeletedWalGenerationError:
            if attempt:
                raise
    raise AssertionError("unreachable")


def replica_tasks(ids: Iterable[str], parent: Optional[str] = None) -> tuple[dict[str, Optional[str]], set[str]]:
    """``({id: created_by}, linked)`` for the ``ids`` any Board replica holds; ``linked`` = those the replica links as
    children of ``parent``. An unreadable replica answers nothing (read-only; a missing file is never created)."""
    want = list(dict.fromkeys(ids))
    found: dict[str, Optional[str]] = {}
    linked: set[str] = set()
    for path in _replica_paths() if want else ():  # nothing to look up: open no file (perf only)
        rest = [i for i in want if i not in found]
        marks = ",".join("?" * len(rest))

        def read(conn: sqlite3.Connection) -> tuple[list, list]:
            return (conn.execute(f"SELECT id, created_by FROM tasks WHERE id IN ({marks})", rest).fetchall(),
                    conn.execute(f"SELECT child_id FROM task_links WHERE parent_id = ? AND child_id IN ({marks})",
                                 (parent, *rest)).fetchall())

        try:
            rows, links = with_reader(path, read)
        except sqlite3.Error:
            continue
        found.update((r[0], r[1]) for r in rows)
        linked.update(r[0] for r in links)
    return found, linked


def replica_prefixes(refs: Iterable[str]) -> set[str]:
    """The short-form ``refs`` (shorter than a full id) that prefix a card id in any Board replica."""
    from hermes_cli.kanban_prose_refs import has_task_with_prefix, is_prefix_ref

    want = [r for r in dict.fromkeys(refs) if is_prefix_ref(r)]
    found: set[str] = set()
    for path in _replica_paths() if want else ():
        try:
            found |= with_reader(path, lambda conn: {r for r in want if r not in found and has_task_with_prefix(conn, r)})
        except sqlite3.Error:
            continue
    return found


# Hub replica task-key lookups, reused while the replica's task rows are unchanged. hermes-hub stamps every replica
# write with its feed's monotonic ``seq`` (see ``kanban_replica_rows``), so an insert or a rewrite raises
# ``max(seq)`` and ``total(seq)``, and a delete lowers ``count``. A row without ``seq`` proves nothing, so then the
# memo is never used. hub.db is written ~45 times a minute (meta, outbox, ledger) while task rows move only when a
# pull lands; the scan this skips (``key GLOB '*/<ref>*'`` over ~20k keys) was ~25 ms per board GET.
_HUB_TASKS_PROBE = "SELECT count(*), count(seq), max(seq), total(seq) FROM replica WHERE coll = 'tasks'"
_HUB_MEMO_REFS = 4096
_hub_lock = threading.Lock()
_hub_memo: dict[str, tuple[tuple, dict[str, bool]]] = {}  # path -> (key, {ref: found}); replaced, never mutated


def _file_ident(path: Path) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def hub_tasks_found(conn: sqlite3.Connection, path: Path, ids: list[str]) -> set[str]:
    """The ``ids`` the hub replica ``conn`` holds as task keys (``kanban_prose_refs.hub_replica_lookup``). Call it
    inside one read transaction on ``conn`` so the probe describes the rows the lookups read.

    Reused answers: all of them while the probe is unchanged. After a pull (same file, ``max(seq)`` not lower) a
    ref known absent can only have appeared in a row written since, so it is re-checked against rows above the old
    ``max(seq)`` alone; a ref known present is looked up again in full (its row may be gone)."""
    from hermes_cli import kanban_prose_refs

    def lookup(want: Iterable[str], after_seq: int | None = None) -> dict[str, bool]:
        return {tid: conn.execute(*kanban_prose_refs.hub_replica_lookup(tid, after_seq)).fetchone() is not None
                for tid in want}

    ident = _file_ident(path)
    try:
        probe = tuple(conn.execute(_HUB_TASKS_PROBE).fetchone())
    except sqlite3.OperationalError:  # an old replica without ``seq``: nothing proves its rows unchanged
        probe = None
    if ident is None or probe is None or probe[0] != probe[1] or probe[2] is None:
        return {tid for tid, hit in lookup(ids).items() if hit}
    # The pathname may have been replaced after this connection opened its
    # snapshot (including before the stat above). Bind answers to the actual
    # borrowed connection too: a reopened reader must never adopt old answers
    # even when both files have identical probe tuples. Keep the object itself,
    # not id(conn), so a later connection cannot reuse a recycled identity.
    ident = (conn, ident)
    key, tag = (ident, probe), str(path)
    with _hub_lock:
        memo = _hub_memo.get(tag)
    want = list(dict.fromkeys(ids))
    if memo is not None and memo[0] == key:
        known = dict(memo[1])
    elif memo is not None and memo[0][0] == ident and probe[2] >= memo[0][1][2]:
        absent = [tid for tid in want if memo[1].get(tid) is False]
        known = {tid: False for tid in absent}
        known.update((tid, True) for tid, hit in lookup(absent, after_seq=memo[0][1][2]).items() if hit)
    else:
        known = {}
    missing = [tid for tid in want if tid not in known]
    known.update(lookup(missing))
    if memo is None or memo[0] != key or missing:
        if len(known) > _HUB_MEMO_REFS:
            known = {tid: known[tid] for tid in want}
        with _hub_lock:
            _hub_memo[tag] = (key, known)
    return {tid for tid in want if known[tid]}


def reset() -> None:
    """Drop the hub lookup memo (tests)."""
    with _hub_lock:
        _hub_memo.clear()


def unresolved_after_replica(missing: list[str]) -> list[str]:
    """``missing`` (not on the local board) minus ids a Board replica or the hub replica knows (a short-form ref by
    prefix); [] when the hub replica cannot say for the rest."""
    known = set(replica_tasks(missing)[0]) | replica_prefixes(missing)
    missing = [p for p in missing if p not in known]
    try:
        from hermes_cli.kanban_session_activity import replica_task_resolution
        found, authoritative = replica_task_resolution(missing)
    except Exception:  # health: allow BLE001 -- advisory scan; an unusable replica means "unknown", never a flag
        return []
    if not authoritative:
        return []
    return [p for p in missing if p not in found]
