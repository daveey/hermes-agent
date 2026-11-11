"""Current-input recovery eligibility; the Board alone changes assignments."""
from __future__ import annotations

import json


def promotion_mode(conn, row, cfg):
    """Legacy local routing was not a machine pin; preserve explicit durable intent.

    An old policy cleared by an owner repin must never be silently re-enrolled.
    Created portability is historical diagnostics, not today's eligibility decision.
    """
    from hermes_cli.kanban_placement import portable_reason

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
    args = dict(row)
    # An allocated scratch directory is not a declared input. Never erase a declared one.
    args["workspace_path"] = created.get("workspace_path")
    args["project_id"] = created.get("project_id")
    args["board"] = "default"
    try:
        for key in ("requires", "skills"):
            args[key] = json.loads(args[key]) if isinstance(args.get(key), str) else args.get(key)
    except (TypeError, ValueError):
        return None
    return "promote" if portable_reason(args, cfg) is None else None
