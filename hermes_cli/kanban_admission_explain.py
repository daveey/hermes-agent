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

    scored = pools.score_enabled()
    out = {"scored": scored, "pools": None}
    for lane in LANES:
        rows = pools.lane_rows(conn, lane)
        # Off the opted-in hub the score is display-only: default weights, never a config read that could refuse.
        parts = {row["id"]: p.as_dict() for row, p in score.rank(conn, rows, weights=None if scored else {})}
        details = {r["id"]: r for r in conn.execute(
            "SELECT id, title, priority, admission_class FROM tasks WHERE status=?", (lane,))}
        out[lane] = [{"id": row["id"], "title": details[row["id"]]["title"],
                      "priority": details[row["id"]]["priority"],
                      "admission_class": details[row["id"]]["admission_class"] or "normal",
                      "score": parts[row["id"]]} for row in rows]
    if scored:
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
    index = ids.index(task_id)
    row = lane[index]
    lines = [f"{task_id}: {task.status} lane position {index + 1}/{len(ids)} (class {row['admission_class']})"]
    if not snap["scored"]:
        lines.append("hub admission score not enabled here: plain priority order")
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
    print("hub admission score order" if snap["scored"] else "plain priority order (hub admission not enabled)")
    if snap["pools"]:
        print(_pools_line(snap["pools"]))
    for lane in LANES:
        print(f"{lane}:")
        for index, row in enumerate(snap[lane], 1):
            print(f"  {index:3d}. {row['id']}  {row['admission_class']:8s}  "
                  f"{row['score']['total']:g}  {row['title']}")
    return 0
