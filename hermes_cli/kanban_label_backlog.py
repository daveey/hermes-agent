"""Read-only same-label queue evidence for pool wait notices and capability-preserving moves. [daveey]

A host-local labels_mismatch is not a fleet placement failure. Report observed same-label claims instead of
advising a capability-dropping move. Counts cover this board view, not guaranteed fleet capacity or pickup time.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Iterable, Mapping, Optional

_log = logging.getLogger(__name__)
WINDOW_SECONDS = 30 * 60
REASON = "matching_pool_busy"
MOVE_REFUSED = "capacity_wait"
ADVICE = ("do not re-place it or drop its labels merely for this wait; recent same-label claims show the placement "
          "works. Check current admission holds if it persists; this is not a pickup-time guarantee")


def _labels(raw: Any) -> Optional[frozenset[str]]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        return None
    return frozenset(value)


def _holder(payload: Any) -> Optional[str]:
    try:
        data = json.loads(payload) if isinstance(payload, str) else (payload or {})
    except ValueError:
        return None
    holder = data.get("holder") if isinstance(data, dict) else None
    return holder if isinstance(holder, str) and holder else None


def available(labels: Iterable[str], machines: Any) -> set[str]:
    """Currently attested matching machines, excluding holds, inactive entries and fresh drains."""
    from hermes_cli.kanban_queue_position import draining
    from hermes_cli.kanban_worker_node import effective_labels
    wanted = set(labels)
    return {name for name, entry in machines.items() if entry and entry.get("state", "active") == "active"
            and not entry.get("hold") and draining(entry) is None and wanted <= effective_labels(entry, name)}


def read(conn: sqlite3.Connection, task_id: str, labels: Iterable[str], *,
         machines: Iterable[str] = (), now: Optional[float] = None,
         machine_labels: Optional[Mapping[str, Iterable[str]]] = None) -> dict[str, Any]:
    """Exact-label ready predecessors and distinct recently claimed cards in this board view.

    Use canonical worker dispatch ordering (including donated priority). Counts are approximate: admission may
    skip a predecessor. Require a Board holder receipt from a currently matching available machine during the
    current ready episode, capped at 30 minutes. Current labels are usable only without a subsequent move/edit;
    ambiguous history is excluded, not guessed.
    Unreadable evidence stays unknown and cannot refuse a move.

    With ``machine_labels`` (matching machine -> its effective labels) the result also carries ``pool_ahead``: ready,
    unclaimed predecessors whose labels one of those machines carries, which it would claim first. Approximate:
    label-less predecessors are not counted. [daveey] t_01a1deaf95f5b885 read "0 ahead" at 13/31 in its pool.
    """
    wanted, holders = frozenset(labels), set(machines)
    pools = None if machine_labels is None else [frozenset(have) for have in machine_labels.values()]
    out: dict[str, Any] = {"ahead": 0, "claimed_recent": 0, "claimers": [], "window_s": WINDOW_SECONDS,
                           "approximate": True, "scope": "board_view", "known": False}
    if not wanted:
        return out
    now = time.time() if now is None else now
    try:
        from hermes_cli.kanban_db_dispatch_queries import lane_rows
        ordered = [row[0] for row in lane_rows(conn, "ready")]
        if task_id not in ordered:
            return out
        from hermes_cli.kanban_parent_unclaimed import ready_since
        created = conn.execute("SELECT created_at FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
        since = max(now - WINDOW_SECONDS, ready_since(conn, task_id, created))
        # Shared-blocker retirement on an already-ready card has no status; only _wake's
        # explicit ready transition starts a new claim-evidence window. Keep this local
        # to the capability guard, without changing the shared notice-age clock.
        from hermes_cli import kanban_db as kb
        for event in conn.execute(
            "SELECT created_at, payload FROM task_events WHERE task_id=? AND kind='shared_blocker_cleared'",
            (task_id,),
        ):
            if kb._json_dict(event[1]).get("status") == "ready":
                since = max(since, event[0])
        before = set(ordered[:ordered.index(task_id)])
        ahead = pool_ahead = 0
        for row in conn.execute("SELECT id, runs_on FROM tasks WHERE status = 'ready' AND claim_lock IS NULL"):
            if row[0] not in before or (have := _labels(row[1])) is None:
                continue
            ahead += have == wanted
            pool_ahead += pools is not None and any(have <= pool for pool in pools)
        claimed: set[str] = set()
        claimers: set[str] = set()
        for row in conn.execute(
            "SELECT e.task_id, e.payload, t.runs_on FROM task_events e JOIN tasks t ON t.id = e.task_id "
            "WHERE e.kind = 'claimed' AND e.created_at >= ? AND e.created_at <= ? AND e.task_id != ? "
            "AND NOT EXISTS (SELECT 1 FROM task_events m WHERE m.task_id=e.task_id "
            "AND m.kind IN ('moved', 'edited') AND m.created_at >= e.created_at)",
            (int(since), int(now), task_id),
        ):
            holder = _holder(row[1])
            if _labels(row[2]) != wanted or holder not in holders:
                continue
            claimed.add(row[0])
            claimers.add(holder)
    except sqlite3.Error:
        _log.debug("label backlog unavailable for %s", task_id, exc_info=True)
        return out
    out.update(ahead=ahead, claimed_recent=len(claimed), claimers=sorted(claimers), known=True)
    if pools is not None:
        out["pool_ahead"] = pool_ahead
    return out


def busy(backlog: Any) -> bool:
    """Recent usable claims contradict a claim that no worker can lease these labels."""
    return isinstance(backlog, dict) and backlog.get("known") is True and int(backlog.get("claimed_recent") or 0) > 0


def words(backlog: Any) -> str:
    if not isinstance(backlog, dict) or not backlog.get("known"):
        return ""
    ahead, claimed = int(backlog.get("ahead") or 0), int(backlog.get("claimed_recent") or 0)
    who = ", ".join(backlog.get("claimers") or [])
    pool = (f"{int(backlog['pool_ahead'] or 0)} ready card(s) ahead that its matching workers can also run; "
            if "pool_ahead" in backlog else "")
    return pool + (f"{ahead} card(s) with matching labels ahead of it (approximate, this board view); "
                   f"matching workers claimed {claimed} such card(s) in the last 30 min"
                   + (f" ({who})" if who and claimed else ""))


def move_refusal(conn: sqlite3.Connection, task_id: str, status: Optional[str], previous: Optional[list[str]],
                 labels: Iterable[str], *, machines: Any, now: Optional[float] = None) -> Optional[str]:
    """Protect queued capabilities when recent usable claims contradict a placement-fault diagnosis."""
    if status != "ready" or not previous:
        return None
    dropped = sorted(lb for lb in set(previous) - set(labels) if lb.startswith("capability:"))
    if not dropped:
        return None
    backlog = read(conn, task_id, previous, machines=available(previous, machines), now=now)
    if not busy(backlog):
        return None
    return (f"this move drops {', '.join(dropped)}, but recent claims contradict a placement fault: {words(backlog)} "
            f"carrying {', '.join(previous)}. A machine without {', '.join(dropped)} cannot do work that needs it. "
            "Pass force only if the card truly does not need the dropped label(s)")
