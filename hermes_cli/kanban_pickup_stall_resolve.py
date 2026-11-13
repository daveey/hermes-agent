"""Close a host-wide pickup repair the detector filed once it has nothing left to repair. [daveey]

t_01a1d5b42df173d3 (2026-11-13): worker-6's "Pickup held on worker-6" repair t_01a1d59589cb8efc named one card,
which ran at 15:55Z; nothing closed the repair, so it sat ``ready`` for a pool run until it was archived by hand at
16:18Z. Every detector pass (:func:`hermes_cli.kanban_pickup_stall.sweep`, stalled cards or not) now archives an
open, unclaimed host-wide repair about this machine (:data:`~hermes_cli.kanban_pickup_stall._HOST_GROUPS`) when

* every card it names (body and the detector's target comments) has left ``ready`` here: done, archived or blocked
  on the Board, gone from the replica, re-pinned elsewhere, leased, or imported into this host's board; or
* it is a ``hold`` repair and this machine's live registry entry no longer holds it: a card still waiting is judged
  on its next pickup skip, not under a hold that ended.

The closing comment names each card's state. The ``pickup_stall_resolved`` event marks the close as the
detector's, so a later stall opens a fresh card instead of reusing this one (``_covering_repair``).
A claimed, running, blocked or in-review repair, or one leased on the Board, is left to its run.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

_log = logging.getLogger(__name__)

EVENT = "pickup_stall_resolved"
_LEAVE = ("running", "review", "blocked")  # an agent has it: its run decides


def open_host_repairs(conn, own: str) -> list[tuple[str, str, list[str]]]:
    """``[(repair id, slug, [target ids in listed order])]`` for every open host-wide repair about ``own``."""
    from hermes_cli import kanban_pickup_stall as kps
    prefix = f"{kps.KEY_PREFIX}{own}:"
    slugs = {spec[0] for spec in kps._HOST_GROUPS.values()}
    out = []
    for row in conn.execute("SELECT id, body, idempotency_key FROM tasks WHERE idempotency_key >= ? "
                            "AND idempotency_key < ? AND status NOT IN ('done', 'archived') ORDER BY created_at, id",
                            (prefix, prefix + "\U0010ffff")).fetchall():
        slug = row["idempotency_key"][len(prefix):].split(":", 1)[0]
        if slug not in slugs:
            continue
        text = (row["body"] or "") + "\n" + "\n".join(
            r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id = ? AND author = ? "
                                       "ORDER BY id", (row["id"], kps.DETECTOR)))
        targets = list(dict.fromkeys(re.findall(r"^- (\S+) \(", text, re.MULTILINE)))
        out.append((row["id"], slug, targets))
    return out


def resolved_by_detector(conn, task_id: str) -> bool:
    return conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? LIMIT 1",
                        (task_id, EVENT)).fetchone() is not None


def _leased(row, now_ms: int) -> Optional[str]:
    keys = row.keys()
    holder = row["lease_holder"] if "lease_holder" in keys else None
    expires = row["lease_expires"] if "lease_expires" in keys else None
    return holder if holder and int(expires or 0) >= now_ms else None


def target_state(conn, replica, own: str, tid: str, now_ms: int) -> tuple[bool, str]:
    """``(still waiting here, state text)`` for one card a repair names."""
    from hermes_cli import kanban_pickup_stall as kps
    from hermes_cli import kanban_worker_node as wn
    local = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    if local is not None:
        return False, f"imported here ({local['status']})"
    row = replica.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    if row is None:
        return False, "not in this host's Board replica"
    if row["status"] != "ready":
        return False, f"{row['status']} on the Board"
    holder = _leased(row, now_ms)
    if holder:
        return False, f"ready on the Board, leased by {holder}"
    if f"machine:{own}" not in (wn._parse(row["runs_on"]) or []):
        return False, f"ready on the Board, re-pinned to {row['runs_on']}"
    skip = kps.latest(conn, tid)
    return True, f"ready, pinned to machine:{own}, not imported (last skip {(skip or {}).get('reason', 'none')})"


def _hold_lifted(own: str) -> Optional[str]:
    """When ``own``'s registry entry is readable and holds nothing, the hold fields as read; else None (held, or
    unreadable: fail toward keeping the alarm)."""
    from hermes_cli import kanban_worker_node as wn
    entry = ((wn.registry() or {}).get("machines") or {}).get(own)
    if not entry or entry.get("hold") is True:
        return None
    return f"state={entry.get('state')!r}, hold={entry.get('hold')!r}, hold_owner={entry.get('hold_owner')!r}"


def _archive(conn, task_id: str, note: str, payload: dict[str, Any]) -> bool:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_archive import archive_task
    from hermes_cli import kanban_pickup_stall as kps

    def precondition(c) -> Optional[str]:
        row = c.execute("SELECT status, claim_lock FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["status"] in ("done", "archived", *_LEAVE) or row["claim_lock"]:
            return "claimed or closed"
        kb._append_event(c, task_id, EVENT, payload)
        return None

    return archive_task(conn, task_id, via=kps.DETECTOR, comment=(kps.DETECTOR, note), precondition=precondition)


def sweep(conn, replica, own: str, *, now_ms: int) -> list[str]:
    """Archive every open host-wide repair about ``own`` that has nothing left to repair; returns their ids."""
    closed = []
    for rid, slug, targets in open_host_repairs(conn, own):
        row = conn.execute("SELECT status, claim_lock FROM tasks WHERE id = ?", (rid,)).fetchone()
        if row is None or row["claim_lock"] or row["status"] in _LEAVE:
            continue
        mine = replica.execute("SELECT * FROM tasks WHERE id = ?", (rid,)).fetchone()
        if mine is not None and _leased(mine, now_ms):
            continue  # a pool run holds it on the Board
        states = [(tid, *target_state(conn, replica, own, tid, now_ms)) for tid in targets]
        if not states:
            continue  # names nothing readable: leave it to its run rather than guess
        lines = "\n".join(f"- {tid}: {text}" for tid, _, text in states)
        lifted = _hold_lifted(own) if slug == "hold" else None
        if not any(waiting for _, waiting, _ in states):
            note = (f"Closed by the {own} pickup-stall detector: every card this repair names has left ready on "
                    f"{own}, so there is nothing left to repair.\n\n{lines}\n\nA later stall opens a fresh card.")
            why = "targets_left_ready"
        elif lifted:
            note = (f"Closed by the {own} pickup-stall detector: {own}'s registry hold has lifted ({lifted}), so "
                    f"this hold repair has nothing left to judge. A card still waiting is judged on its next pickup "
                    f"skip, under that reason's own alarm.\n\n{lines}")
            why = "hold_lifted"
        else:
            continue
        payload = {"why": why, "targets": {tid: text for tid, _, text in states}}
        if _archive(conn, rid, note, payload):
            closed.append(rid)
            _log.info("pickup-stall closed card=%s why=%s targets=%d", rid, why, len(states))
    return closed
