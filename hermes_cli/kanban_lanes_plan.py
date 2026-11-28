"""Validate and apply a lane planner's action list. [daveey]

The planner is an LLM run (a cron job or a chat) that reads ``hermes kanban lanes show --json`` and proposes actions;
nothing it says is trusted. :func:`apply` is the only writer: it applies the actions below within fixed limits and
refuses everything else, one refusal per action, so a bad plan degrades to its valid part.

==============  ==================================================================================
``admit``       a gate-held card to ready, at most the lane's ``admit_per_day`` per local day, never a
                title matching the lane's ``never_admit``
``merge``       a gate-held card into an open card of the same lane: a link and a comment; the merged
                card stays held and is never archived
``reprioritize``  0..1000, never a pinned card
``relane``      only a card whose lane came from ``inherited``, ``unclassified`` or ``llm``
``note``        one line per lane, kept for the panel
==============  ==================================================================================
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_lanes as lanes

PINNED, UNPINNED, MERGED = "lane_pinned", "lane_unpinned", "lane_merged"
MAX_ACTIONS = 50
ACTOR = "lane-planner"
_MOVABLE = ("inherited", "unclassified", "llm")
_OPEN = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review")


def notes_path() -> Path:
    from hermes_constants import get_default_hermes_root

    return Path(get_default_hermes_root()) / "kanban" / "lanes-planner.json"


def pinned(conn, task_id: str) -> bool:
    row = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND kind IN (?, ?) ORDER BY id DESC LIMIT 1",
        (task_id, PINNED, UNPINNED)).fetchone()
    return row is not None and row[0] == PINNED


def set_pin(conn, task_id: str, on: bool, *, actor: str) -> None:
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        if kb._task_status(conn, task_id) is None:
            raise ValueError(f"no such task: {task_id}")
        kb._append_event(conn, task_id, PINNED if on else UNPINNED, {"actor": actor})


def lane_source(conn, task_id: str) -> Optional[str]:
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                       (task_id, lanes.ASSIGNED)).fetchone()
    return None if row is None else json.loads(row[0] or "{}").get("source")


def _card(conn, task_id):
    if not isinstance(task_id, str):
        return None
    return conn.execute("SELECT id, title, tenant, status, block_kind, priority FROM tasks WHERE id = ?",
                        (task_id,)).fetchone()


def _held(conn, row) -> bool:
    return conn.execute(f"SELECT 1 FROM tasks WHERE id = ? AND {lanes.HELD}", (row["id"],)).fetchone() is not None


def _admit(conn, pol, action, row, state) -> Optional[str]:
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn, allow_nested=True):
        return _admit_locked(conn, pol, action, row, state)


def _admit_locked(conn, pol, action, row, state) -> Optional[str]:
    if not _held(conn, row):
        return "card is not held at a lane gate"
    lane = pol.lanes[pol.lane_of(row["tenant"])]
    if any(p.search(row["title"] or "") for p in lane.never_admit):
        return f"lane {lane.name} never auto-admits this kind of card"
    used = state["used"].setdefault(lane.name, lanes.admitted_today(conn, lane.name, now=state["now"]))
    if lane.admit_per_day is not None and used >= lane.admit_per_day:
        return f"lane {lane.name} admit budget spent ({used}/{lane.admit_per_day} today)"
    from hermes_cli.kanban_lane_daily_limits import reason, row_opts
    why = reason(conn, pol, lane.name, row_opts(conn, row), now=state["now"])
    if why:
        return why
    lanes.admit(conn, row["id"], actor=state["actor"], reason=action["reason"])
    state["used"][lane.name] = used + 1
    return None


def _merge(conn, pol, action, row, _state) -> Optional[str]:
    from hermes_cli import kanban_db as kb

    target = _card(conn, action.get("into"))
    if not _held(conn, row):
        return "only a gate-held card can be merged"
    if target is None or target["id"] == row["id"] or target["status"] not in _OPEN:
        return "merge target must be another open card"
    if pol.lane_of(target["tenant"]) != pol.lane_of(row["tenant"]):
        return "merge target is in another lane"
    with kb.write_txn(conn):
        kb.link_tasks(conn, target["id"], row["id"], allow_nested=True)
        kb._append_event(conn, row["id"], MERGED, {"into": target["id"], "reason": action["reason"]})
    kb.add_comment(conn, target["id"], ACTOR, f"Also covers {row['id']} ({row['title']}): {action['reason']}",
                   system=True)
    return None


def _reprioritize(conn, _pol, action, row, _state) -> Optional[str]:
    from hermes_cli import kanban_db as kb

    value = action.get("priority")
    if type(value) is not int or not 0 <= value <= 1000:
        return "priority must be an integer from 0 to 1000"
    if row["status"] not in _OPEN:
        return "card is not open"
    if pinned(conn, row["id"]):
        return "card is pinned"
    kb.edit_task(conn, row["id"], priority=value, priority_reason=f"{ACTOR}: {action['reason']}")
    return None


def _relane(conn, pol, action, row, _state) -> Optional[str]:
    lane = action.get("lane")
    if lane not in pol.lanes:
        return f"unknown lane: {lane}"
    if lane_source(conn, row["id"]) not in _MOVABLE:
        return "lane was given or set by a rule; only the user moves it"
    if pol.lanes[lane].gated or pol.lanes[pol.lane_of(row["tenant"])].gated:
        return "a move into or out of a gated lane is the user's"
    lanes.assign(conn, row["id"], lane, source="llm", reason=action["reason"], pol=pol)
    return None


def _admit_continuation(conn, pol, action, row, state):
    from hermes_cli.kanban_acceptance_continuations import admit
    return admit(conn, pol, action, row, state)


def _archive(conn, pol, action, row, state):
    from hermes_cli.kanban_lanes_archive import archive
    return archive(conn, pol, action, row, state)


_HANDLERS = {"archive": _archive, "admit": _admit, "admit_continuation": _admit_continuation,
             "merge": _merge, "reprioritize": _reprioritize, "relane": _relane}


def apply(conn, actions, *, now: Optional[float] = None, dry_run: bool = False, actor: str = ACTOR) -> dict:
    """Apply a planner's ``actions``; returns ``{"applied": [...], "rejected": [{..., "why"}], "notes": [...]}``.

    ``actor`` is recorded on admissions (the planner by default; an agent's own ``lanes admit`` names itself).
    ``dry_run`` validates the shape only (budget and card checks need the writes of earlier actions): its
    ``applied`` list is what passed that check, and ``dry_run`` is true in the result.
    """
    pol = lanes.policy()
    if pol is None:
        raise ValueError("no lanes are configured (kanban.admission.lanes)")
    if not isinstance(actions, list):
        raise ValueError("actions must be a list")
    out: dict = {"applied": [], "rejected": [], "notes": [], "dry_run": dry_run}
    state: dict = {"now": time.time() if now is None else now, "used": {}, "actor": actor}
    for index, action in enumerate(actions):
        why = _refusal(conn, pol, action, index, state, out, dry_run)
        if why:
            out["rejected"].append({"action": action, "why": why})
    return out


def _refusal(conn, pol, action, index, state, out, dry_run) -> Optional[str]:
    if index >= MAX_ACTIONS:
        return f"over the {MAX_ACTIONS}-action limit for one plan"
    if not isinstance(action, dict):
        return "an action must be an object"
    kind = action.get("action")
    reason = action.get("reason")
    if kind == "note":
        if action.get("lane") not in pol.lanes or not isinstance(action.get("text"), str):
            return "a note names a configured lane and carries text"
        out["notes"].append({"lane": action["lane"], "text": action["text"][:300]})
        return None
    if kind not in _HANDLERS:
        return f"unknown action: {kind}"
    if not isinstance(reason, str) or not reason.strip():
        return "a one-line reason is required"
    row = _card(conn, action.get("card"))
    if row is None:
        return "no such card"
    if not dry_run:
        try:
            why = _HANDLERS[kind](conn, pol, action, row, state)
        except (ValueError, RuntimeError) as exc:  # a refused link or edit is this action's refusal, not the plan's
            why = str(exc)
        if why:
            return why
    out["applied"].append(action)
    return None


def save_notes(result: dict, *, now: Optional[float] = None) -> None:
    """Keep the latest plan's notes and counts where the panel reads them."""
    path = notes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"at": int(time.time() if now is None else now), "notes": result["notes"],
           "applied": len(result["applied"]), "rejected": len(result["rejected"])}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    tmp.replace(path)


def read_notes() -> Optional[dict]:
    try:
        return json.loads(notes_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
