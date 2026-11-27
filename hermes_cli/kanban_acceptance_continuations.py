"""Explicit executor attestations of required acceptance; never infer necessity from prose.

Only canonical completion of an owned claimed run admits a declaration. The body
reference validates attribution, not semantic truth. Lane admission remains a
separate, budgeted transaction; this never renews scope or clears another hold.
"""
from __future__ import annotations

import hashlib
import json

KEY = "acceptance_continuations"
_SCOPE = ("title", "body", "tenant", "assignee", "workspace_kind", "workspace_path", "branch_name",
          "project_id", "skills", "requires", "runs_on", "completion_contract", "review_required",
          "goal_mode", "goal_max_turns", "model_override", "provider_override", "max_runtime_seconds")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _body(row):
    return hashlib.sha256((row["body"] or "").encode()).hexdigest()


def source_digest(conn, task_id):
    return _body(conn.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone())


def _scope(row):
    return _digest({k: row[k] for k in _SCOPE if k in row.keys()})


def _object(payload):
    try:
        value = json.loads(payload or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _created(conn, child):
    row = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='created' ORDER BY id LIMIT 1",
                       (child,)).fetchone()
    return (row["uid"] if "uid" in row.keys() else None, _object(row["payload"])) if row else (None, {})


def _lineage(conn, parent, child):
    uid, created = _created(conn, child)
    linked = conn.execute("SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?", (parent, child)).fetchone()
    return uid if created.get("creator_task_id") == parent and linked else None


def _hold(conn, row):
    from hermes_cli.kanban_containment_connections import refusal
    from hermes_cli.kanban_spend_decision import pending, execution_restricted
    from hermes_cli.kanban_refusal_receipt import restricted
    if row["human_request"]:
        return "existing human/physical ask; named human owns that step"
    if why := refusal(conn, row["id"]):
        return f"existing protection: {why}; existing protection owner must resolve it"
    if restricted(conn, row["id"]) or execution_restricted(conn, row["id"]) or pending(conn, row["id"]):
        return "scope/safety/spend restriction; existing decision owner must resolve it"
    # This new path only admits never-executed children. No new run window is granted.
    if conn.execute("SELECT 1 FROM task_runs WHERE task_id=? LIMIT 1", (row["id"],)).fetchone():
        return "child already has execution history; creator must disposition remaining work"
    return None


def prepare(conn, parent_id, metadata, expected_run_id, *, force=False):
    """Called inside complete_task's transaction, before ending the run. Returns writer-owned evidence."""
    if not isinstance(metadata, dict) or KEY not in metadata:
        return None
    raw = metadata[KEY]
    parent = conn.execute("SELECT * FROM tasks WHERE id=?", (parent_id,)).fetchone()
    run = conn.execute("SELECT * FROM task_runs WHERE task_id=? AND id=?", (parent_id, expected_run_id)).fetchone()
    claimed = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='claimed' AND run_id=?",
                           (parent_id, expected_run_id)).fetchone()
    if (force or expected_run_id is None or parent is None or parent["current_run_id"] != expected_run_id
            or run is None or run["ended_at"] is not None or not claimed or "uid" not in run.keys() or not run["uid"]):
        raise ValueError("acceptance_continuations: current owned genuinely claimed run required")
    if (not isinstance(raw, dict) or set(raw) != {"version", "source_body_sha256", "items"}
            or type(raw["version"]) is not int or raw["version"] != 1
            or raw["source_body_sha256"] != _body(parent)
            or not isinstance(raw["items"], list) or not 1 <= len(raw["items"]) <= 16):
        raise ValueError("acceptance_continuations: exact parent body digest and v1 items required")
    _, original = _created(conn, parent_id)
    if original.get("source_body_sha256") != _body(parent):
        raise ValueError("acceptance_continuations: unchanged original creation scope required")
    items, seen = [], set()
    for item in raw["items"]:
        if (not isinstance(item, dict) or set(item) != {"card", "criterion"}
                or not isinstance(item["card"], str) or item["card"] in seen
                or not isinstance(item["criterion"], str) or not 16 <= len(item["criterion"].strip()) <= 2000
                or item["criterion"] not in (parent["body"] or "")):
            raise ValueError("acceptance_continuations: unique child and literal source criterion required")
        child = conn.execute("SELECT * FROM tasks WHERE id=?", (item["card"],)).fetchone()
        uid = _lineage(conn, parent_id, item["card"])
        if (child is None or not uid
                or child["status"] in ("done", "archived") or _hold(conn, child)):
            raise ValueError("acceptance_continuations: open unprotected created dependency required")
        seen.add(item["card"])
        items.append({**item, "created_uid": uid, "scope_sha256": _scope(child)})
    return {"version": 1, "source_body_sha256": _body(parent), "run_uid": run["uid"], "items": items}


def check(conn, child_id):
    """Read-only current evidence; apply calls it again under its write lock. Returns reason and source."""
    from hermes_cli import kanban_lanes as lanes
    child = conn.execute("SELECT * FROM tasks WHERE id=?", (child_id,)).fetchone()
    # Like ordinary lanes apply, this acts on this board's synchronized rows; creation
    # namespace is not executor custody (a pool worker can run a hub-created card).
    if child is None:
        return "board evidence unavailable; owning board must evaluate", None
    if why := _hold(conn, child):
        return why, None
    if not conn.execute(f"SELECT 1 FROM tasks WHERE id=? AND {lanes.HELD}", (child_id,)).fetchone():
        return "not held solely at a lane gate; existing lifecycle owner acts next", None
    _, created = _created(conn, child_id)
    parent_id = created.get("creator_task_id")
    parent = conn.execute("SELECT * FROM tasks WHERE id=?", (parent_id,)).fetchone() if isinstance(parent_id, str) else None
    if parent is None or not _lineage(conn, parent_id, child_id):
        return "authenticated creator dependency missing; creator must attest required acceptance", None
    if parent["status"] != "done":
        return "creator parent not genuinely complete; planner reevaluates after completion", None
    if conn.execute("SELECT 1 FROM task_links l JOIN tasks t ON t.id=l.parent_id WHERE l.child_id=? "
                    "AND t.status!='done'", (child_id,)).fetchone():
        return "unsatisfied or archived dependency; dependency owner acts next", None
    event = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='completed' ORDER BY id DESC LIMIT 1",
                         (parent_id,)).fetchone()
    run = conn.execute("SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (parent_id,)).fetchone()
    if not event or not run or event["run_id"] != run["id"] or run["outcome"] != "completed":
        return "latest genuine completion missing; creator must disposition acceptance", None
    proof = _object(event["payload"]).get(KEY)
    if (not isinstance(proof, dict) or type(proof.get("version")) is not int or proof.get("version") != 1
            or "uid" not in run.keys() or not run["uid"] or "uid" not in event.keys() or not event["uid"]
            or proof.get("run_uid") != run["uid"] or not isinstance(proof.get("items"), list)
            or not all(isinstance(i, dict) for i in proof["items"])):
        return "typed completion attestation missing; creator must explicitly attest or request manual admission", None
    if proof.get("source_body_sha256") != _body(parent):
        return "parent acceptance changed; creator must disposition changed scope", None
    item = next((i for i in proof.get("items", []) if i.get("card") == child_id), None)
    if not item or item.get("created_uid") != _lineage(conn, parent_id, child_id):
        return "required acceptance evidence missing; unsolicited work stays held for creator", None
    if item.get("scope_sha256") != _scope(child):
        return "child scope changed; creator must disposition changed scope", None
    return None, {"parent": parent_id, "completed_uid": event["uid"], "run_uid": run["uid"]}


def candidates(conn, rows=None):
    """Every local held card has an explicit next actor/reason, not a fabricated human ask."""
    from hermes_cli import kanban_lanes as lanes
    out = []
    for row in lanes.held(conn) if rows is None else rows:
        why, source = check(conn, row["id"])
        out.append({"card": row["id"], "lane": row["tenant"], "eligible": why is None,
                    "next_actor": "lane-planner" if why is None else f"existing-owner:{row['id']}",
                    "reason": why or "typed required acceptance; existing lane budget and holds still apply",
                    "source": source})
    return out


def admit(conn, pol, action, row, state):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_lanes as lanes
    from hermes_cli.kanban_lanes_plan import _admit_locked
    with kb.write_txn(conn, allow_nested=True):
        why, source = check(conn, row["id"])
        if why:
            return why
        if pol.lanes[pol.lane_of(row["tenant"])].paused:
            return "lane paused; user owns resumption"
        lane = pol.lane_of(row["tenant"])
        state["used"][lane] = lanes.admitted_today(conn, lane, now=state["now"])
        return _admit_locked(conn, pol, {**action, "reason": f"{action['reason']} (completion {source['completed_uid']})"},
                             row, state)
