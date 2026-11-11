"""Current-input recovery eligibility; the Board alone changes assignments."""
from __future__ import annotations

import json
import math
import time


def request_assignment(hub, board, row, mode, candidates):
    """Keep recovery opt-in; ordinary reservations retain their existing wire contract."""
    args = {"task_id": row["id"], "expected_revision": row["sync_version"], "candidates": candidates}
    return hub.recover(board, mode=mode, **args) if mode else hub.reserve(board, **args)


def unchanged_ready(conn, row):
    """Recheck the selected snapshot after preceding network calls, without a network-spanning lock."""
    current = conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
    if current is None or dict(current) != dict(row):
        return False
    return not conn.execute(
        "SELECT 1 FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=? "
        "AND state IN ('pending','rejected') LIMIT 1", (row["id"],)).fetchone()


def recovery_mode(conn, row, cfg):
    """Propose a stale-pin recovery only from an affirmative registry observation."""
    from hermes_cli import kanban_worker_node as node
    from hermes_cli.kanban_worker_placement import requested_move

    if row.get("placement_policy") and requested_move(conn, row["id"]) is not None:
        return None  # an unapplied owner move takes precedence over automatic recovery
    if row.get("runs_on") is None:
        return promotion_mode(conn, row, cfg) if row.get("placement_policy") is None else None
    try:
        labels = node.normalize_runs_on(row["runs_on"])
    except (TypeError, ValueError):
        return None
    names = [label.removeprefix("machine:") for label in labels or [] if label.startswith("machine:")]
    if len(names) != 1:
        return None
    entry = ((node.registry() or {}).get("machines") or {}).get(names[0])
    if not isinstance(entry, dict):
        return None
    seen = entry.get("last_seen")
    stale = (type(seen) in (int, float) and math.isfinite(seen) and seen > 0
             and time.time() * 1000 - seen > 30 * 60 * 1000)
    if entry.get("state") not in ("revoked", "inactive") and not entry.get("hold") and not stale:
        return None
    return "stale_pin" if current_portable(conn, row, cfg) else None


def current_portable(conn, row, cfg):
    from hermes_cli.kanban_placement import portable_reason

    # Even scratch work can contain unpublished outputs from an earlier run.
    if row.get("workspace_path") or conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id=? LIMIT 1", (row["id"],)).fetchone():
        return False
    args = dict(row)
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created' "
                         "ORDER BY id LIMIT 1", (row["id"],)).fetchone()
    try:
        created = json.loads(event[0]) if event else None
        if not isinstance(created, dict):
            return False
        # Allocated scratch path is not an input, but declared paths/projects remain constraints.
        args["workspace_path"] = created.get("workspace_path")
        args["project_id"] = row.get("project_id") or created.get("project_id")
        args["board"] = "default"
        args["runs_on"] = None  # Board preserves/rechecks the replacement's other labels.
        for key in ("requires", "skills"):
            args[key] = json.loads(args[key]) if isinstance(args.get(key), str) else args.get(key)
            if args[key] is not None and (not isinstance(args[key], list)
                                         or not all(isinstance(v, str) for v in args[key])):
                return False
        return portable_reason(args, cfg) is None
    except (TypeError, ValueError):
        return False


def promotion_mode(conn, row, cfg):
    """Legacy local routing was not a machine pin; preserve explicit durable intent.

    An old policy cleared by an owner repin must never be silently re-enrolled.
    Created portability is historical diagnostics, not today's eligibility decision.
    """
    if row.get("runs_on") is not None:
        return None
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created' "
                         "ORDER BY id LIMIT 1", (row["id"],)).fetchone()
    if event is None:
        return None
    try:
        created = json.loads(event[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(created, dict) or created.get("placement_intent") == "explicit" or created.get("placement_policy"):
        return None
    if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind IN "
                    "('moved','placement_repinned') LIMIT 1", (row["id"],)).fetchone():
        return None
    return "promote" if current_portable(conn, row, cfg) else None
