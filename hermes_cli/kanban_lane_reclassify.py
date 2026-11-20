"""Re-evaluate default/inherited lane labels without changing execution custody. [daveey]"""
from __future__ import annotations

from hermes_cli import kanban_lanes as lanes


def reclassify(conn, *, apply: bool = False) -> list[dict]:
    """Dry run by default; existing admissions/holds, priorities and terminal history stay intact.

    Rules can relabel a previously unclassified/inherited default-lane card, including
    into a gated lane: this is accounting repair, not admission or gate release.
    Explicit user/planner assignments and prior rule decisions are never overwritten.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_lanes_plan as plan

    pol = lanes.policy()
    if pol is None:
        raise ValueError("no lanes configured")
    changes = []
    with kb.write_txn(conn):
        rows = conn.execute(
            "SELECT * FROM tasks WHERE tenant = ? AND status NOT IN ('done', 'archived') ORDER BY created_at, id",
            (pol.default,)).fetchall()
        for row in rows:
            if plan.lane_source(conn, row["id"]) not in ("unclassified", "inherited"):
                continue
            opts = dict(row)
            opts["_stored_card"] = True
            if opts.get("project_id"):
                _project, _obj, repo, _kind = kb._resolve_project_link(
                    conn, opts["project_id"], None, opts["workspace_kind"], opts["workspace_path"])
                opts["project_repo"] = repo
            lane = lanes._rule_lane(pol, opts)
            if lane is None or lane == pol.default:
                continue
            change = {"id": row["id"], "title": row["title"], "from": row["tenant"], "lane": lane,
                      "status": row["status"]}
            changes.append(change)
            if apply:
                conn.execute("UPDATE tasks SET tenant = ? WHERE id = ?", (lane, row["id"]))
                kb._append_event(conn, row["id"], lanes.ASSIGNED,
                                 {"lane": lane, "from": row["tenant"], "source": "rule",
                                  "reason": "re-evaluated stored provenance; existing admission/holds unchanged"})
    return changes


def command(args) -> int:
    import json
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing() as conn:
        changes = reclassify(conn, apply=args.apply)
    print(json.dumps({"applied": args.apply, "changes": changes}, indent=2))
    return 0
