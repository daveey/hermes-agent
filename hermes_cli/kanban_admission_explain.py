"""``hermes kanban queue``: hub admission order with score breakdowns (``--explain <id>`` for one card). [daveey]"""
from __future__ import annotations

import argparse

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_output import _json_out

LANES = ("review", "ready")


def snapshot(conn) -> dict:
    """Both lanes in dispatch order, each row with its score breakdown, plus the pool census when opted in."""
    from hermes_cli import kanban_admission_pools as pools
    from hermes_cli import kanban_admission_score as score
    from hermes_cli import kanban_queue_position as qp

    from hermes_cli.kanban_db_dispatch import review_dispatch_enabled

    out = {"scored": True, "pools": None}
    combined = []
    for lane in LANES:
        rows = pools.lane_rows(conn, lane)
        # All dispatchers use the same configured scoring policy.
        parts = {row["id"]: p.as_dict() for row, p in score.rank(conn, rows)}
        details = {r["id"]: r for r in conn.execute(
            "SELECT id, title, priority, admission_class FROM tasks WHERE status=?", (lane,))}
        out[lane] = [{"id": row["id"], "title": details[row["id"]]["title"],
                      "priority": details[row["id"]]["priority"],
                      "admission_class": details[row["id"]]["admission_class"] or "normal",
                      "score": parts[row["id"]]} for row in rows]
        if lane == "ready" or review_dispatch_enabled():
            combined.extend(rows)
    by_id = {row["id"]: dict(row, lane=lane) for lane in LANES for row in out[lane]}
    out["dispatch_order"] = [dict(by_id[row["id"]], score=parts.as_dict())
                             for row, parts in score.rank(conn, combined)]
    if pools.score_enabled():
        _running, limit = qp._running_and_limit(conn, None)
        out["pools"] = pools.queue_fields(limit, "normal").get("admission_pools")
    return out


def _pools_line(census) -> str:
    def part(name):
        count, cap = census[name], census[name + "_cap"]
        return f"{name} {'?' if count is None else count}/{'?' if cap is None else cap}"
    return "pools: " + ", ".join(part(name) for name in ("regular", "priority"))


def _explain(conn, snap, task_id) -> list[str]:
    from hermes_cli.kanban_admission_score import explain

    task = kb.get_task(conn, task_id)
    if task is None:
        return [f"no such task: {task_id}"]
    lane = snap.get(task.status) if task.status in LANES else None
    ids = [row["id"] for row in lane or ()]
    if lane is None or task_id not in ids:
        return [f"{task_id}: not in a ready or review lane (status {task.status})"]
    lane_index = ids.index(task_id)
    row = lane[lane_index]
    combined = snap["dispatch_order"]
    ids = [candidate["id"] for candidate in combined]
    if task_id not in ids:
        return [f"{task_id}: review dispatch disabled", explain(row["score"])]
    index = ids.index(task_id)
    lines = [f"{task_id}: combined position {index + 1}/{len(ids)} "
             f"({task.status} lane position {lane_index + 1}/{len(lane)}; class {row['admission_class']})",
             "advisory score order; placement, holds and capacity may exclude candidates"]

    lines.append(explain(row["score"]))
    if snap["pools"]:
        lines.append(_pools_line(snap["pools"]))
    if index:
        lines.append("ahead: " + ", ".join(ids[:min(index, 10)]) + (" …" if index > 10 else ""))
    return lines


def cmd_queue(args: argparse.Namespace) -> int:
    if getattr(args, "report", False):
        from hermes_cli import kanban_admission_report as report

        with kbc.connect_closing() as conn:
            data = report.capacity_report(conn, days=args.days)
        if not _json_out(args, data):
            print(report.render(data))
        return 0
    with kbc.connect_closing() as conn:
        snap = snapshot(conn)
        if args.explain:
            lines = _explain(conn, snap, args.explain)
            print("\n".join(lines))
            return 1 if lines[0].startswith("no such task") else 0
    if _json_out(args, snap):
        return 0
    print("admission score order (combined lanes; eligibility and capacity still apply)")
    if snap["pools"]:
        print(_pools_line(snap["pools"]))
    for index, row in enumerate(snap["dispatch_order"], 1):
        print(f"  {index:3d}. {row['id']}  {row['lane']:6s}  {row['admission_class']:8s}  "
              f"{row['score']['total']:g}  {row['title']}")
    return 0
