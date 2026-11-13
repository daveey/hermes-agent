"""A pooled card that parks with unpushed state stays on the worker that holds it. [daveey]

A card on the worker queue (``runs_on: [pool:worker]``, :mod:`kanban_move_pool`) can be resumed by any worker. When
its run parks (``kanban_continue``, ``kanban_schedule``, ``kanban_block`` and the time and dependency parks it routes
to) with a dirty tree or commits on no remote, another worker resuming it would start without that state. So the
park pins it to ``machine:<this worker>`` in the same transaction and records the state in a ``pool_pinned`` event.
A later park of that card whose trees are all clean and pushed (or gone) puts it back on the pool
(``pool_unpinned``). A clean, pushed tree never pins.

The git checks run before the park's write transaction (:func:`plan`); :func:`apply` only writes, and only while the
same run is still current. Only the gateway that holds the run (its ``claim_lock`` has this host's prefix) plans a
pin: a hub-side block of a card running on a worker has no view of that worker's trees.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

PINNED_EVENT = "pool_pinned"
UNPINNED_EVENT = "pool_unpinned"


def plan(conn: sqlite3.Connection, task_id: str, text: Optional[str] = "") -> Optional[dict[str, Any]]:
    """What the park of ``task_id``'s current run should do to its placement, or None (nothing)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_move_pool as kmp
    from hermes_cli import kanban_worker_node as wn
    try:
        if "runs_on" not in sync.table_columns(conn, "tasks") or sync.board_mode(conn) != "primary":
            return None
        row = conn.execute("SELECT status, current_run_id, claim_lock, runs_on, workspace_path FROM tasks "
                           "WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["status"] != "running" or not row["current_run_id"]:
            return None
        if not str(row["claim_lock"] or "").startswith(kb._host_prefix()):
            return None
        labels = wn._parse(row["runs_on"]) or []
        me = str(sync._state(conn)["gateway_id"])
    except (sqlite3.Error, KeyError, TypeError):
        return None
    label = kmp.pool_label()
    base = {"task_id": task_id, "run_id": int(row["current_run_id"]), "me": me, "runs_on": labels}
    if label in labels:
        trees, state = kmp.card_trees_state(task_id, row["workspace_path"], text, linked_text_only=True)
        return {**base, "op": "pin", "state": state, "trees": trees} if state else None
    pinned = _last_pin(conn, task_id)
    if labels != [f"machine:{me}"] or pinned is None or pinned.get("runs_on") != labels:
        return None
    # A text-named shared main checkout (the gateway's live ~/.hermes/hermes-agent) is not the card's: an older pin
    # that recorded one in ``trees`` lifts once the card's own trees are clean and pushed.
    trees, state = kmp.card_trees_state(task_id, row["workspace_path"], text, extra=pinned.get("trees") or [],
                                        linked_text_only=True)
    return None if state else {**base, "op": "unpin", "restore": pinned.get("previous_runs_on") or [label],
                               "trees": trees}


def _last_pin(conn: sqlite3.Connection, task_id: str) -> Optional[dict[str, Any]]:
    """Payload of the newest placement event when it is a park pin (a later move or unpin supersedes it)."""
    row = conn.execute("SELECT kind, payload FROM task_events WHERE task_id = ? AND kind IN ('moved', ?, ?) "
                       "ORDER BY id DESC LIMIT 1", (task_id, PINNED_EVENT, UNPINNED_EVENT)).fetchone()
    if row is None or row["kind"] != PINNED_EVENT:
        return None
    try:
        data = json.loads(row["payload"] or "{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def apply(conn: sqlite3.Connection, task_id: str, step: Optional[dict[str, Any]]) -> bool:
    """Inside the park's write transaction: write the planned placement while the planned run is still current."""
    if not step:
        return False
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_move_pool as kmp
    from hermes_cli import kanban_worker_node as wn
    row = conn.execute("SELECT current_run_id, runs_on FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["current_run_id"] != step["run_id"] or (wn._parse(row["runs_on"]) or []) != step["runs_on"]:
        return False
    me = step["me"]
    if step["op"] == "pin":
        target = [f"machine:{me}"]
        check, tree = kmp.state_parts(step["state"])
        payload = {"from_machine": me, "runs_on": target, "previous_runs_on": step["runs_on"],
                   "unpushed_state": step["state"], "trees": step["trees"], "check": check, "tree": tree,
                   "host_reason": f"parked with unpushed state on {me}",
                   "reason": f"{step['state']}: another worker would resume without it"}
        kind = PINNED_EVENT
    else:
        target = list(step["restore"])
        payload = {"from_machine": me, "runs_on": target, "previous_runs_on": step["runs_on"], "trees": step["trees"],
                   "reason": f"every tree is clean and pushed (or gone): back on the pool from {me}"}
        kind = UNPINNED_EVENT
    wn.set_runs_on(conn, task_id, target)
    kb._append_event(conn, task_id, kind, payload, run_id=step["run_id"])
    return True
