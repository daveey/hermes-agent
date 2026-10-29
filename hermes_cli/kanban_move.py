"""A running card moves itself to another machine: ``kanban_move`` / ``hermes kanban move``. [daveey]

A move is a ``kanban_continue`` whose re-queue also rewrites the card's ``runs_on``. In one write transaction it
saves the checkpoint, ends this run as ``continued``, writes the new labels and appends a ``moved`` event. The
card goes back to ``ready``; when the holder pushes it out of ``running`` the Board ends its lease (worker
``rows.ts``), and the Board's grant checks ``runs_on`` labels for every claimer, the creator included
(``checkPlacement``), so only a matching machine runs it next. Nothing runs twice: the run ends before the lease
does, and the lease CAS admits one claimer.

Guards:

* the target labels must be satisfiable now by a registry machine other than this one that is active and not
  on hold (``machine:<x>`` for an unregistered x never is);
* ``kanban.move.max_moves`` (default 3) moves per card;
* moving back to a machine the card already left is ping-pong: refused unless ``force``, and it still counts;
* ``dir`` / ``worktree`` workspaces exist only on their creator, so those cards never move;
* the card's ``requires`` must fit the assignee's profile on at least one matching machine whose profile
  tools are known or unknown; every match known to lack one refuses ``capability_mismatch``, naming the
  machine and the missing tool (:mod:`hermes_cli.kanban_runs_on_capabilities`);
* the board must sync to the Board service (``runs_on`` is a Board placement field).

Every move is a ``moved`` event ``{from_machine, runs_on, previous_runs_on, reason, move_no, forced}``: the audit log.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Optional

DEFAULT_MAX_MOVES = 3
MOVED_EVENT = "moved"


class MoveRefused(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def max_moves(cfg: Optional[dict] = None) -> int:
    if cfg is None:
        try:
            from hermes_cli.config import load_config_readonly
            cfg = ((load_config_readonly() or {}).get("kanban") or {}).get("move") or {}
        except Exception:
            cfg = {}
    try:
        return max(1, int((cfg or {}).get("max_moves", DEFAULT_MAX_MOVES)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_MOVES


def history(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
                        (task_id, MOVED_EVENT)).fetchall()
    out = []
    for (payload,) in rows:
        try:
            out.append(json.loads(payload) if payload else {})
        except ValueError:
            out.append({})
    return out


def matching_machines(runs_on: list[str], machines: Mapping[str, Mapping[str, Any]], *, me: str) -> list[str]:
    """Registry machines other than ``me`` that are active, not held and carry every label."""
    from hermes_cli import kanban_worker_node as wn
    out = []
    for name, entry in machines.items():
        if name == me or not entry or entry.get("state", "active") != "active" or entry.get("hold"):
            continue
        if set(runs_on) <= wn.effective_labels(entry, name):
            out.append(name)
    return sorted(out)


def check(conn: sqlite3.Connection, task_id: str, runs_on: Any, *, force: bool = False,
          machines: Optional[Mapping[str, Mapping[str, Any]]] = None, me: Optional[str] = None,
          cfg: Optional[dict] = None) -> dict[str, Any]:
    """Validate a move; returns ``{runs_on, previous, matches, move_no, forced, me}`` or raises :class:`MoveRefused`."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn
    try:
        labels = wn.normalize_runs_on(runs_on)
    except ValueError as exc:
        raise MoveRefused("invalid_runs_on", str(exc)) from exc
    if not labels:
        raise MoveRefused("invalid_runs_on", "runs_on must name at least one label, e.g. ['machine:worker-1']")
    row = conn.execute("SELECT status, workspace_kind, runs_on, assignee, requires FROM tasks WHERE id = ?",
                       (task_id,)).fetchone()
    if row is None:
        raise MoveRefused("not_found", f"task {task_id} not found")
    if row["workspace_kind"] in ("dir", "worktree"):
        raise MoveRefused("workspace_bound", f"a {row['workspace_kind']} card runs only where its workspace is")
    if sync.board_mode(conn) != "primary":
        raise MoveRefused("not_synced", "runs_on needs a board that syncs to the Board service (kanban.sync)")
    me = me or str(sync._state(conn)["gateway_id"])
    if machines is None:
        reg = wn.registry()
        if reg is None:
            raise MoveRefused("registry_unavailable", "the Board machine registry is unreachable; try again")
        machines = reg.get("machines") or {}
    matches = matching_machines(labels, machines, me=me)
    if not matches:
        raise MoveRefused("no_machine", f"no active, unheld machine other than {me} has every label in {labels}")
    from hermes_cli import kanban_runs_on_capabilities as roc
    try:
        requires = json.loads(row["requires"]) if row["requires"] else []
    except ValueError:
        requires = []
    try:
        roc.check(labels, assignee=row["assignee"], requires=requires, workspace_kind=row["workspace_kind"] or "scratch",
                  me=me, machines={m: machines[m] for m in matches}, exclude_me=True)
    except roc.PlacementCapabilityMismatch as exc:
        raise MoveRefused("capability_mismatch", str(exc)) from exc
    past = history(conn, task_id)
    limit = max_moves(cfg)
    if len(past) >= limit:
        raise MoveRefused("move_limit", f"card already moved {len(past)} time(s) (kanban.move.max_moves {limit}); "
                                        "finish it here or block it")
    left = {p.get("from_machine") for p in past}
    back = [m for m in matches if m in left]
    if back and len(back) == len(matches) and not force:
        raise MoveRefused("ping_pong", f"the card already left {', '.join(back)}; pass force to move it back")
    return {"runs_on": labels, "previous": wn._parse(row["runs_on"]), "matches": matches, "move_no": len(past) + 1,
            "forced": bool(back and force), "me": me}


def move_task(conn: sqlite3.Connection, task_id: str, *, runs_on: Any, reason: str, checkpoint: str,
              expected_run_id: Optional[int] = None, force: bool = False,
              machines: Optional[Mapping[str, Mapping[str, Any]]] = None, me: Optional[str] = None,
              cfg: Optional[dict] = None) -> dict[str, Any]:
    """Move a RUNNING card: ``{"ok": True, "runs_on", "matches", "move_no", "run_id", "status"}`` or
    ``{"ok": False, "error", "reason_code"}``."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli.kanban_continuation import continue_task
    reason = (reason or "").strip()
    if not reason:
        return {"ok": False, "reason_code": "reason_required", "error": "reason is required (why this card must move)"}
    try:
        plan = check(conn, task_id, runs_on, force=force, machines=machines, me=me, cfg=cfg)
    except MoveRefused as exc:
        return {"ok": False, "reason_code": exc.code, "error": str(exc)}

    def _in_txn(c: sqlite3.Connection, run_id: int) -> None:
        wn.set_runs_on(c, task_id, plan["runs_on"])
        kb._append_event(c, task_id, MOVED_EVENT, {
            "from_machine": plan["me"], "runs_on": plan["runs_on"], "previous_runs_on": plan["previous"],
            "reason": reason[:500], "move_no": plan["move_no"], "forced": plan["forced"],
            "matches": plan["matches"]}, run_id=run_id)

    note = f"Moving to {plan['runs_on']} ({', '.join(plan['matches'])}): {reason}"
    res = continue_task(conn, task_id, checkpoint=f"{checkpoint.strip()}\n\n[{note}]",
                        expected_run_id=expected_run_id, in_txn=_in_txn)
    if not res.get("ok"):
        return {"ok": False, "reason_code": res.get("reason_code") or "not_running", "error": res.get("error")}
    return {"ok": True, "runs_on": plan["runs_on"], "matches": plan["matches"], "move_no": plan["move_no"],
            "run_id": res.get("run_id"), "status": res.get("status")}


def move_queued(conn: sqlite3.Connection, task_id: str, *, runs_on: Any, reason: str, force: bool = False,
                machines: Optional[Mapping[str, Mapping[str, Any]]] = None, me: Optional[str] = None,
                cfg: Optional[dict] = None) -> dict[str, Any]:
    """Move a card that is NOT running (ready/todo/scheduled/blocked/triage): same guards, no run to end."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_worker_node as wn
    reason = (reason or "").strip()
    if not reason:
        return {"ok": False, "reason_code": "reason_required", "error": "reason is required (why this card must move)"}
    try:
        plan = check(conn, task_id, runs_on, force=force, machines=machines, me=me, cfg=cfg)
    except MoveRefused as exc:
        return {"ok": False, "reason_code": exc.code, "error": str(exc)}
    with kb.write_txn(conn):
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row["status"] in ("running", "done", "archived"):
            return {"ok": False, "reason_code": "bad_status",
                    "error": f"task {task_id} is {row['status']}" + (" (use --reclaim, or kanban_move from its run)"
                                                                   if row["status"] == "running" else "")}
        wn.set_runs_on(conn, task_id, plan["runs_on"])
        kb._append_event(conn, task_id, MOVED_EVENT, {
            "from_machine": plan["me"], "runs_on": plan["runs_on"], "previous_runs_on": plan["previous"],
            "reason": reason[:500], "move_no": plan["move_no"], "forced": plan["forced"],
            "matches": plan["matches"], "queued": True})
    return {"ok": True, "runs_on": plan["runs_on"], "matches": plan["matches"], "move_no": plan["move_no"],
            "status": row["status"]}


def move_reclaim(conn: sqlite3.Connection, task_id: str, *, runs_on: Any, reason: str, force: bool = False,
                 machines: Optional[Mapping[str, Mapping[str, Any]]] = None, me: Optional[str] = None,
                 cfg: Optional[dict] = None) -> dict[str, Any]:
    """Operator move of a card running on THIS gateway (``hermes kanban move --reclaim``): write the new labels
    first (``runs_on`` is not lease-fenced), then reclaim the run (stops its worker) and release the Board lease.
    Labels first means a re-claim between the two steps already sees the new placement."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn
    reason = (reason or "").strip()
    if not reason:
        return {"ok": False, "reason_code": "reason_required", "error": "reason is required (why this card must move)"}
    try:
        plan = check(conn, task_id, runs_on, force=force, machines=machines, me=me, cfg=cfg)
    except MoveRefused as exc:
        return {"ok": False, "reason_code": exc.code, "error": str(exc)}
    row = conn.execute("SELECT t.status, t.claim_lock, r.uid AS run_uid FROM tasks t LEFT JOIN task_runs r "
                       "ON r.id = t.current_run_id WHERE t.id = ?", (task_id,)).fetchone()
    if row["status"] != "running" or row["claim_lock"] is None:
        return {"ok": False, "reason_code": "not_running_here",
                "error": f"task {task_id} is not running under a claim on this gateway"}
    with kb.write_txn(conn):
        wn.set_runs_on(conn, task_id, plan["runs_on"])
        kb._append_event(conn, task_id, MOVED_EVENT, {
            "from_machine": plan["me"], "runs_on": plan["runs_on"], "previous_runs_on": plan["previous"],
            "reason": reason[:500], "move_no": plan["move_no"], "forced": plan["forced"],
            "matches": plan["matches"], "reclaimed": True})
    if not kb.reclaim_task(conn, task_id, reason=f"moved to {plan['runs_on']}: {reason}"[:400]):
        return {"ok": False, "reason_code": "reclaim_failed",
                "error": f"labels set to {plan['runs_on']} but the run could not be reclaimed; it ends on its own"}
    sync.release_lease(conn, task_id, row["run_uid"])
    status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]
    return {"ok": True, "runs_on": plan["runs_on"], "matches": plan["matches"], "move_no": plan["move_no"],
            "status": status}
