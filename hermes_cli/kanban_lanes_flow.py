"""Open-card flow for ``hermes kanban lanes flow --json``. [daveey]

Steps are derived from status, card events and an explicit PR snapshot. Missing evidence is
``landing_unclassified``, never a guessed landing stage. Daily ``cards_per_day`` / ``usd_per_day``
caps are enforced at creation in ``kanban_lanes``; this module only reports them.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from hermes_cli import kanban_lanes as lanes

WINDOWS = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400}
STEPS = ("intake", "waiting", "ready", "running", "review", "ci", "merge", "deploy", "test", "done")
LANDING_UNCLASSIFIED = "landing_unclassified"
LIMIT_CARDS = "limit:cards_per_day"
LIMIT_USD = "limit:usd_per_day"
_LANDING_STEPS = {"review", "ci", "merge", "deploy", "test"}
_USER = lanes.USER_CREATORS
_OPEN = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review")


def _payload(event: dict) -> dict:
    raw = event.get("payload")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _review_active(events) -> bool:
    for event in reversed(events or ()):
        if event.get("kind") in ("review_requested", "changes_requested", "review_changes_requested"):
            return event.get("kind") == "review_requested"
    return False


def _landing_evidence(card: dict) -> bool:
    if card.get("status") == "review":
        return True
    if _review_active(card.get("events")):
        return True
    pr = card.get("pr")
    if isinstance(pr, dict) and pr:
        return True
    awaited = card.get("awaiting") or {}
    return isinstance(awaited, dict) and awaited.get("kind") in ("ci", "deploy")


def _landing_step(card: dict) -> str:
    pr = card.get("pr") if isinstance(card.get("pr"), dict) else {}
    pr = pr or {}
    awaited = card.get("awaiting") if isinstance(card.get("awaiting"), dict) else {}
    awaited = awaited or {}
    review_event = card.get("status") == "review" or _review_active(card.get("events"))
    checks = pr.get("checks")
    if checks is None and awaited.get("kind") == "ci":
        checks = "pending"
    merged = pr.get("merged")
    deployed = pr.get("deployed")
    if deployed is None and awaited.get("kind") == "deploy":
        merged = True if merged is None else merged
        deployed = False
    verified = pr.get("verified")
    approved = pr.get("approved")
    if merged is True and deployed is True and verified is False:
        return "test"
    if merged is True and deployed is not True:
        return "deploy"
    if approved is True and checks == "green" and merged is False:
        return "merge"
    if checks == "pending":
        return "ci"
    if review_event:
        return "review"
    return LANDING_UNCLASSIFIED


def classify_step(card: dict) -> str:
    status = card.get("status")
    if status == "done":
        return "done"
    if status == "running":
        return "running"
    if _landing_evidence(card):
        return _landing_step(card)
    if status == "ready":
        return "ready"
    if status in ("blocked", "scheduled", "todo"):
        return "waiting"
    if status == "triage":
        return "intake"
    return LANDING_UNCLASSIFIED


def _skip_reason(card: dict) -> Optional[str]:
    for event in reversed(card.get("events") or ()):
        if event.get("kind") != "dispatch_skipped":
            continue
        reason = _payload(event).get("reason") or event.get("reason")
        detail = str(_payload(event).get("detail") or "")
        if reason == "llm_paced" or "llm_paced" in detail:
            return "quota"
        if reason in ("waiting_for_worker_capacity", "skipped_ineligible") or "waiting_for_worker_capacity" in detail:
            return "slot"
    return None


def classify_reason(card: dict, step: str) -> str:
    if card.get("human_request") or card.get("block_kind") in ("needs_input", "capability"):
        return "you"
    gate_reason = card.get("gate_reason")
    if gate_reason in (LIMIT_CARDS, LIMIT_USD):
        return gate_reason
    held = card.get("held", card.get("status") == "triage" and card.get("block_kind") == lanes.GATE_KIND)
    if held:
        return "gate"
    if card.get("parents_open"):
        return "dep"
    if card.get("status") == "scheduled":
        return "time"
    skip = _skip_reason(card)
    if skip:
        return skip
    if step in _LANDING_STEPS:
        return "review"
    if step == "running":
        return "run"
    if step == "ready":
        return "slot"
    if step == "waiting":
        return "time" if card.get("status") == "scheduled" else "dep"
    if step == "intake":
        return "triage"
    if step == "done":
        return "run"
    return "triage"


def classify_source(card: dict) -> dict[str, Optional[str]]:
    session_id = card.get("session_id") or None
    created = card.get("created_by") or ""
    project = card.get("project_id") or None
    if session_id:
        return {"kind": "session", "id": session_id, "project": project}
    if creator := card.get("creator_task_id"):
        return {"kind": "automation", "id": creator, "project": project}
    if created in _USER:
        return {"kind": "user", "id": created, "project": project}
    if created:
        return {"kind": "automation", "id": created, "project": project}
    return {"kind": "user", "id": "", "project": project}


def _json_obj(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _pr_url(task, events) -> Optional[str]:
    contract = task["completion_contract"] if "completion_contract" in task.keys() else None
    if isinstance(contract, str) and "pull/" in contract:
        return contract
    for event in reversed(events):
        payload = _json_obj(event["payload"] if not isinstance(event, dict) else event.get("payload"))
        if not isinstance(payload, dict):
            continue
        meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else payload
        for key in ("published_pr", "reviewed_pr", "pr"):
            url = meta.get(key) if isinstance(meta, dict) else None
            if isinstance(url, str) and url.startswith("http"):
                return url
    return None


def _pr_snapshot(task, events, awaiting, reader=None) -> Optional[dict]:
    snap: dict[str, Any] = {}
    url = _pr_url(task, events)
    if not url and isinstance(awaiting, dict):
        ref = awaiting.get("ref")
        if isinstance(ref, str) and ref.startswith("https://github.com/") and "/pull/" in ref:
            url = ref
    if url:
        snap["url"] = url
    if isinstance(awaiting, dict) and awaiting.get("kind") == "ci":
        snap["checks"] = "pending"
    if isinstance(awaiting, dict) and awaiting.get("kind") == "deploy":
        snap.update(merged=True, deployed=False)
    # Deployment has no provider-independent API. Accept explicit task-event observations only;
    # never turn a merge receipt or elapsed time into proof of live installation.
    for event in events:
        meta = _payload(dict(event)).get("metadata")
        observed = meta.get("flow_pr") if isinstance(meta, dict) else None
        if isinstance(observed, dict) and url and observed.get("url") == url:
            for key in ("merged", "deployed", "verified"):
                if type(observed.get(key)) is bool:
                    snap[key] = observed[key]
    if reader is not None and url:
        snap.update(reader.get(url))
    return snap or None


def _gate_reason(events) -> Optional[str]:
    for event in reversed(events):
        kind = event["kind"] if not isinstance(event, dict) else event.get("kind")
        if kind != lanes.GATED:
            continue
        payload = _json_obj(event["payload"] if not isinstance(event, dict) else event.get("payload")) or {}
        reason = payload.get("reason") if isinstance(payload, dict) else None
        return reason if reason in (LIMIT_CARDS, LIMIT_USD) else None
    return None


def _events(conn, task_id: str) -> list:
    return conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id", (task_id,)).fetchall()


def _parents_open(conn, task_id: str) -> bool:
    from hermes_cli import kanban_db as kb

    return bool(kb.unsatisfied_parents(conn, task_id))


def _awaiting(task, events=()) -> Optional[dict]:
    if task["status"] != "scheduled":
        return None
    for event in reversed(events):
        if event["kind"] == "scheduled":
            data = _payload(dict(event)).get("awaiting")
            return data if isinstance(data, dict) else None
    return None


def _card_from_row(conn, task, now: float, reader=None, pol=None) -> dict:
    events = _events(conn, task["id"])
    awaited = _awaiting(task, events)
    held = conn.execute(f"SELECT 1 FROM tasks WHERE id=? AND {lanes.HELD}", (task["id"],)).fetchone() is not None
    gate_reason = _gate_reason(events) if held else None
    if gate_reason and pol is not None:
        from hermes_cli.kanban_lane_daily_limits import reason
        gate_reason = reason(conn, pol, task["tenant"], dict(task), now=now)
    creator = next((_payload(dict(e)).get("creator_task_id") for e in events if e["kind"] == "created"), None)
    human = _json_obj(task["human_request"] if "human_request" in task.keys() else None)
    age = max(0.0, (now - float(task["created_at"] or now)) / 3600.0)
    pr = _pr_snapshot(task, events, awaited, reader)
    event_dicts = [{"kind": e["kind"], "payload": e["payload"]} for e in events]
    return {
        "id": task["id"], "title": task["title"], "status": task["status"],
        "block_kind": task["block_kind"], "human_request": human if human else None,
        "parents_open": _parents_open(conn, task["id"]), "events": event_dicts,
        "awaiting": awaited, "pr": pr, "created_by": task["created_by"],
        "session_id": task["session_id"] if "session_id" in task.keys() else None,
        "project_id": task["project_id"] if "project_id" in task.keys() else None,
        "held": held, "gate_reason": gate_reason,
        "creator_task_id": creator,
        "age_h": round(age, 1), "tenant": task["tenant"],
    }


def snapshot(conn, *, window: str = "6h", now: Optional[float] = None, pol=None) -> dict:
    """Cards still open, plus per-lane window throughput and today's cap usage."""
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {', '.join(WINDOWS)}")
    now = time.time() if now is None else now
    pol = lanes.safe_policy() if pol is None else pol
    if pol is None:
        pol = lanes.parse({"lanes": {"other": {}}})
    window_s = WINDOWS[window]
    usage = lanes.usage(conn, pol, window_s=window_s, now=now)
    rows = conn.execute(
        f"SELECT * FROM tasks WHERE status IN {_OPEN} ORDER BY created_at, id").fetchall()
    from hermes_cli.kanban_lanes_flow_pr import Reader
    reader = Reader()
    cards = []
    for row in rows:
        card = _card_from_row(conn, row, now, reader, pol)
        step = classify_step(card)
        reason = classify_reason(card, step)
        source = classify_source(card)
        pr = None
        if isinstance(card.get("pr"), dict):
            pr = card["pr"].get("url")
        cards.append({"id": card["id"], "title": card["title"] or "",
                      "lane": pol.lane_of(card["tenant"]), "step": step, "reason": reason,
                      "source": source, "age_h": card["age_h"], "pr": pr})
    out_lanes = {}
    for name, spec in pol.lanes.items():
        row = usage[name]
        out_lanes[name] = {
            "done": row["done"],
            "run_hours": round(row["run_seconds"] / 3600, 1),
            "cost_usd": row["cost_usd"],
            "limits": {"cards_per_day": spec.cards_per_day, "usd_per_day": spec.usd_per_day},
            "usage": {"cards_today": lanes.cards_today(conn, name, now=now),
                      "usd_today": lanes.spent_today(conn, pol, name, now=now)},
        }
    return {"window": window, "cards": cards, "lanes": out_lanes}
