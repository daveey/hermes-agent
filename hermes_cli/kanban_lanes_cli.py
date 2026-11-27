"""``hermes kanban lanes``: see and steer the split of worker time between project lanes. [daveey]

``show`` (the default) prints target against actual share and what is backed up per lane; ``held`` and
``unclassified`` list the cards a planner decides about; ``assign``, ``admit``, ``pin`` and ``set`` are the user's
controls; ``apply`` runs a planner's action list through ``kanban_lanes_plan.apply``.
"""
from __future__ import annotations

import argparse
import json
import sys

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_lanes as lanes
from hermes_cli.kanban_output import _json_out


def _fleet(conn, local: bool):
    """The connection the report reads: the board's full replica when there is one (fleet-wide), else ``conn``."""
    if local:
        return None
    from hermes_cli import kanban_worker_node as wn

    import sqlite3

    try:
        return wn.open_replica(conn)
    except (RuntimeError, sqlite3.Error, OSError):
        return None


def snapshot(conn, *, hours: float, local: bool = False, evidence=None) -> dict:
    from hermes_cli import kanban_lanes_plan as plan

    pol = lanes.policy()
    if pol is None:
        return {"configured": False}
    if evidence is None:
        from hermes_cli.kanban_lanes_sources import read_view
        with read_view(conn, local=local) as evidence:
            return snapshot(conn, hours=hours, local=local, evidence=evidence)
    rows = lanes.usage(evidence.conn, pol, window_s=int(hours * 3600), evidence=evidence)
    for name, lane in pol.lanes.items():
        rows[name]["describe"] = lane.describe
    return {"configured": True, "window_hours": hours, "scope": "fleet" if evidence.conn is not conn else "local",
            "default": pol.default, "agent_priority_cap": pol.priority_cap, "shadow": pol.shadow, "lanes": rows,
            "planner": plan.read_notes()}


def _pct(value: float) -> str:
    return f"{round(value * 100):3d}%"


def render(snap: dict) -> str:
    if not snap["configured"]:
        return "no lanes configured (kanban.admission.lanes)"
    mode = "; shadow: reporting only, nothing is held or reordered" if snap.get("shadow") else ""
    lines = [f"lanes over the last {snap['window_hours']:g} h ({snap['scope']}{mode})",
             f"{'lane':12} {'target':>6} {'actual':>6} {'run-h':>7} {'running':>7} {'ready':>5} {'held':>5} "
             f"{'open':>5}  notes"]
    for name, row in snap["lanes"].items():
        notes = []
        if row["paused"]:
            notes.append("paused")
        if row["gated"]:
            budget = "no limit" if row["admit_per_day"] is None else f"{row['admitted_today']}/{row['admit_per_day']}"
            notes.append(f"gated, admitted today {budget}")
        if row["ready"] == 0 and row["target"] > row["actual"]:
            notes.append("under target: nothing ready")
        lines.append(f"{name:12} {_pct(row['target']):>6} {_pct(row['actual']):>6} "
                     f"{row['run_seconds'] / 3600:7.1f} {row['running']:7d} {row['ready']:5d} {row['held']:5d} "
                     f"{row['open']:5d}  {'; '.join(notes)}")
    return "\n".join(lines)


def _show(args) -> int:
    with kbc.connect_closing() as conn:
        snap = snapshot(conn, hours=args.hours, local=args.local)
    if not _json_out(args, snap):
        print(render(snap))
    return 0


def _cards(args, rows, *, continuations=None) -> int:
    data = [{"id": r["id"], "title": r["title"], "lane": r["tenant"], "priority": r["priority"],
             "created_at": r["created_at"], "created_by": r["created_by"]} for r in rows]
    if continuations is not None:
        for card in data:
            card["continuation"] = continuations[card["id"]]
    if not _json_out(args, data):
        print("\n".join(f"{c['id']}  [{c['lane']}]  {c['title']}" for c in data) or "none")
    return 0


def _held(args) -> int:
    from hermes_cli.kanban_acceptance_continuations import candidates
    with kbc.connect_closing() as conn:
        rows = lanes.held(conn, args.lane)[:args.limit]
        return _cards(args, rows, continuations={c["card"]: c for c in candidates(conn, rows)})


def _unclassified(args) -> int:
    with kbc.connect_closing() as conn:
        rows = conn.execute(
            "SELECT t.id, t.title, t.tenant, t.priority, t.created_at, t.created_by FROM tasks t "
            "WHERE t.status NOT IN ('done', 'archived') AND ? = (SELECT json_extract(e.payload, '$.source') "
            "FROM task_events e WHERE e.task_id = t.id AND e.kind = ? ORDER BY e.id DESC LIMIT 1) "
            "ORDER BY t.created_at LIMIT ?", ("unclassified", lanes.ASSIGNED, args.limit)).fetchall()
        return _cards(args, rows)


def _assign(args) -> int:
    with kbc.connect_closing() as conn:
        lanes.assign(conn, args.task_id, args.lane, source=args.source, reason=args.reason or "")
    print(f"{args.task_id}: lane {args.lane}")
    return 0


def _admit(args) -> int:
    """The person's admit is theirs to make and outside the daily budget. An agent's goes through the same
    limits as a planner's (``admit_per_day``, ``never_admit``) and spends the budget."""
    from hermes_cli.kanban_cli_actor import cli_actor

    reason = args.reason or ""
    with kbc.connect_closing() as conn:
        if lanes.cli_person(args):
            done = lanes.admit(conn, args.task_id, actor="user", reason=reason, budget=False)
        else:
            from hermes_cli import kanban_lanes_plan as plan

            actor = cli_actor()
            result = plan.apply(conn, [{"action": "admit", "card": args.task_id,
                                        "reason": reason or f"admitted by {actor}"}], actor=actor)
            if result["rejected"]:
                print(f"{args.task_id}: {result['rejected'][0]['why']}")
                return 1
            done = True
    print(f"{args.task_id}: {'admitted' if done else 'not held at a lane gate'}")
    return 0 if done else 1


def _pin(args) -> int:
    from hermes_cli import kanban_lanes_plan as plan
    from hermes_cli.kanban_cli_actor import cli_actor

    with kbc.connect_closing() as conn:
        plan.set_pin(conn, args.task_id, not args.off, actor=cli_actor())
    print(f"{args.task_id}: {'unpinned' if args.off else 'pinned'}")
    return 0


UNSET = object()  # omitted is distinct from explicit null (clear a nullable limit)


def set_lane(lane: str, *, share=None, admit_per_day=None, cards_per_day=UNSET, usd_per_day=UNSET,
             max_workers=UNSET,
             paused=None) -> None:
    """Change one lane's share, daily admit/cards/spend budget or pause state; raises ValueError.

    Written to the installation-root ``config.yaml``, the file the policy is read from, whatever profile the
    caller runs under: a profile's own config never holds lanes.
    """
    import copy
    from pathlib import Path

    from hermes_constants import get_default_hermes_root
    from hermes_cli.config import atomic_config_write
    from hermes_cli.config_backend import read_config_doc_readonly

    pol = lanes.policy()
    if pol is None or lane not in pol.lanes:
        raise ValueError(f"unknown lane: {lane}")
    import math

    if share is not None and (type(share) not in (int, float) or not math.isfinite(share) or share < 0):
        raise ValueError("share must be a finite nonnegative number")
    if admit_per_day is not None and (type(admit_per_day) is not int or admit_per_day < 0):
        raise ValueError("admit_per_day must be a nonnegative integer")
    if cards_per_day is not UNSET and cards_per_day is not None and (type(cards_per_day) is not int or cards_per_day < 0):
        raise ValueError("cards_per_day must be a nonnegative integer")
    if usd_per_day is not UNSET and usd_per_day is not None and (type(usd_per_day) not in (int, float) or not math.isfinite(usd_per_day)
                                    or usd_per_day < 0):
        raise ValueError("usd_per_day must be a finite nonnegative number")
    if paused is not None and type(paused) is not bool:
        raise ValueError("paused must be true or false")
    if max_workers is not UNSET and max_workers is not None and (type(max_workers) is not int or max_workers < 0):
        raise ValueError("max_workers must be a nonnegative integer")
    changes = {"share": share, "admit_per_day": admit_per_day, "paused": paused}
    changes = {key: value for key, value in changes.items() if value is not None}
    changes.update({key: value for key, value in {"cards_per_day": cards_per_day,
                    "usd_per_day": usd_per_day, "max_workers": max_workers}.items() if value is not UNSET})
    if not changes:
        raise ValueError("nothing to set: give a share, a daily admit/cards/spend budget, pause or resume")
    path = Path(get_default_hermes_root()) / "config.yaml"
    doc = copy.deepcopy(read_config_doc_readonly(path))
    spec = doc["kanban"]["admission"]["lanes"]
    if spec[lane] is None:
        spec[lane] = {}
    spec[lane].update(changes)
    lanes.parse(doc["kanban"]["admission"])  # refuse to write a mapping the policy would reject
    atomic_config_write(path, doc)


def _set(args) -> int:
    limits = {key: getattr(args, key, UNSET) for key in ("cards_per_day", "usd_per_day", "max_workers")}
    for key in getattr(args, "unset", None) or []:
        if limits[key] is not UNSET:
            raise ValueError(f"cannot set and unset {key} together")
        limits[key] = None
    set_lane(args.lane, share=args.share, admit_per_day=args.admit_per_day,
             **limits,
             paused=True if args.pause else False if args.resume else None)
    return 0


def _flow(args) -> int:
    from hermes_cli import kanban_lanes_flow as flow
    from hermes_cli.kanban_lanes_sources import read_view

    with kbc.connect_closing() as conn:
        with read_view(conn) as evidence:
            data = flow.snapshot(evidence.conn, window=args.window, evidence=evidence)
    if not _json_out(args, data):
        print(json.dumps(data, indent=2))
    return 0


def _apply(args) -> int:
    from hermes_cli import kanban_lanes_plan as plan

    raw = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8-sig").read()
    with kbc.connect_closing() as conn:
        result = plan.apply(conn, json.loads(raw), dry_run=args.dry_run)
    if not args.dry_run:
        plan.save_notes(result)
    if not _json_out(args, result):
        print(f"applied {len(result['applied'])}, rejected {len(result['rejected'])}, "
              f"notes {len(result['notes'])}")
        for item in result["rejected"]:
            print(f"  rejected: {item['why']}: {json.dumps(item['action'])[:160]}")
    return 0


def _explain(args) -> int:
    from hermes_cli import kanban_lanes_plan as plan

    with kbc.connect_closing() as conn:
        pol = lanes.policy()
        row = conn.execute(f"SELECT id, title, tenant, status, ({lanes.HELD}) AS held FROM tasks WHERE id = ?",
                           (args.task_id,)).fetchone()
        if pol is None or row is None:
            print("no lanes configured" if pol is None else f"no such task: {args.task_id}")
            return 1
        lane = pol.lanes[pol.lane_of(row["tenant"])]
        load = lanes._running(conn, pol)
        data = {"id": row["id"], "lane": lane.name, "source": plan.lane_source(conn, row["id"]),
                "status": row["status"], "held_at_gate": bool(row["held"]),
                "paused": lane.paused, "pinned": plan.pinned(conn, row["id"]),
                "running_per_share": {name: (round(load[name] / spec.share, 3) if spec.share else None)
                                      for name, spec in pol.lanes.items()}}
    if not _json_out(args, data):
        state = ("held at the lane gate until admitted" if data["held_at_gate"] else
                 "lane is paused" if lane.paused else f"status {row['status']}")
        ratios = ", ".join(f"{name} {'-' if value is None else value}"
                           for name, value in data["running_per_share"].items())
        print(f"{row['id']}: lane {lane.name} (source {data['source'] or 'none'}); {state}\n"
              f"next free slot goes to the lowest running/share with ready work: {ratios}")
    return 0


_ACTIONS = {None: _show, "show": _show, "flow": _flow, "held": _held, "unclassified": _unclassified,
            "assign": _assign, "admit": _admit, "pin": _pin, "set": _set, "apply": _apply, "explain": _explain}


def command(args: argparse.Namespace) -> int:
    action = getattr(args, "lanes_action", None)
    if action is None:
        args.hours, args.local = 6.0, False
    try:
        if action == "reclassify":
            from hermes_cli.kanban_lane_reclassify import command as reclassify_command

            return reclassify_command(args)
        return _ACTIONS[action](args)
    except ValueError as exc:
        print(f"lanes: {exc}", file=sys.stderr)
        return 2
