"""[daveey] Cards this gateway pushed after its replica's last completed pull (t_01a1cfedbfe95b4f).

A sync round pulls the replica (the whole board) first and pushes the live outbox second, so a row this gateway
just pushed reaches the replica only with the next round's pull. A worker copy evicted in between and copied back
from the replica lost it: a crashed PR owner's ``Opened <PR>`` comment and its ``reclaimed`` event were pushed in
one round, the next tick evicted the copy (empty outbox, no ``respawn_guarded`` recorded yet) and re-imported it
without them, ``active_pr`` did not hold and the owner was respawned with no handoff.

The sync loop (``_pulled_replica``, ``push``) and the dispatcher (``kanban_worker_node.pickup``) run in one
gateway process, so the record is in memory, keyed by the replica's path. A process that never pushed (a CLI
dispatch) sees nothing and behaves as before; an entry older than ``MAX_LAG_S`` is dropped, so a replica that
never catches up (pull failing) cannot pin a copy forever.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Iterable, Optional

_log = logging.getLogger(__name__)
MAX_LAG_S = 120.0

_LOCK = threading.Lock()
_STARTED: dict[str, int] = {}  # replica path -> generation of the newest pull started
_DONE: dict[str, int] = {}  # replica path -> newest generation whose pull completed
_ACKED: dict[str, dict[str, tuple[int, float]]] = {}  # replica path -> task id -> (generation at ack, when)


def _monotonic() -> float:
    return time.monotonic()


def reset() -> None:
    """Tests: forget every record."""
    with _LOCK:
        _STARTED.clear()
        _DONE.clear()
        _ACKED.clear()


def pull_started(path) -> int:
    """A replica pull begins; returns its generation for :func:`pull_done`."""
    key = str(path)
    with _LOCK:
        _STARTED[key] = _STARTED.get(key, 0) + 1
        return _STARTED[key]


def pull_done(path, generation: int) -> None:
    """The pull of ``generation`` completed: it holds every row the Board acknowledged before it started."""
    key = str(path)
    with _LOCK:
        _DONE[key] = max(_DONE.get(key, 0), generation)


def acked(path, task_ids: Iterable[Optional[str]]) -> None:
    """The Board acknowledged rows of ``task_ids``; only a pull started after now can hold them."""
    key = str(path)
    now = _monotonic()
    with _LOCK:
        generation = _STARTED.get(key, 0)
        cards = _ACKED.setdefault(key, {})
        for tid in task_ids:
            if tid:
                cards[str(tid)] = (generation, now)


def lagging(path, task_ids: Iterable[str]) -> set[str]:
    """Of ``task_ids``, those with a push the replica at ``path`` does not hold yet."""
    key = str(path)
    now = _monotonic()
    with _LOCK:
        cards = _ACKED.get(key)
        if not cards:
            return set()
        done = _DONE.get(key, 0)
        for tid in [t for t, (gen, at) in cards.items() if gen < done or now - at > MAX_LAG_S]:
            del cards[tid]
        return {t for t in task_ids if t in cards}


def acked_task_ids(rows: Iterable[dict]) -> list[str]:
    """The card each acknowledged outbox row belongs to (``tasks`` rows are the card; children carry task_id)."""
    import json
    out: list[Optional[str]] = []
    for row in rows:
        if row.get("table_name") == "tasks":
            out.append(row.get("row_uid"))
            continue
        fields = row.get("fields") or {}
        try:
            fields = fields if isinstance(fields, dict) else json.loads(fields)
        except (TypeError, ValueError):
            continue
        if isinstance(fields, dict):
            out.append(fields.get("task_id") or fields.get("child_id"))
    return [t for t in out if t]


def held_copies(conn: sqlite3.Connection, path, ids: list[str]) -> set[str]:
    """Copies in ``ids`` the local guard holds while the replica lacks this gateway's push for them: evicting
    one now would re-import it without the rows the hold rests on. Kept until the replica catches up, when the
    recorded-hold rule (``kanban_worker_node._guard_held``) decides as before."""
    from hermes_cli import kanban_db_dispatch as dispatch
    held: set[str] = set()
    for tid in lagging(path, ids):
        try:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
            if row is None or row["status"] not in ("ready", "review"):
                continue
            if dispatch.check_respawn_guard(conn, tid, lane=row["status"]) is not None:
                held.add(tid)
        except Exception:  # health: allow BLE001 -- a failed check evicts as before; the lease still decides
            _log.warning("kanban replica lag: guard check for copy %s failed", tid, exc_info=True)
    return held
