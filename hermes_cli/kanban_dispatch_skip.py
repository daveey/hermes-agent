"""Durable reasons for a ready/review card a dispatcher tick passed over without claiming it. [daveey]

Defect kanban-dd62aef001 (hub, 2026-11-10): t_01a1c7a0c8139180 (bb-tuner, pinned claude-opus-5-5/anthropic) sat
ready 33 min with free worker slots while another bb-tuner card was claimed. The card carried no ``claimed``,
``skipped_*`` or ``respawn_guarded`` event, so the chat-wait and parent-unclaimed notices fell back to "no worker
(1 card(s) ahead of it, 4/7 worker slots busy)" and suggested a priority bump. The bump did nothing; an agent
guessed a quota wall and parked the card. The tick logs had rotated, so the real skip site was lost.

* :func:`record_tick` (the dispatcher's post-result seam, ``kanban_capacity_evidence.observe_dispatch``) appends
  ONE ``dispatch_skipped {reason, detail, until?}`` event per (card, reason, detail) per ready episode for every
  row the tick passed over: ``skipped_ineligible``, ``capsule_refused``, ``no_progress_held``,
  ``per_profile_capped``, ``claim_deferred`` (Board/claim refusals), ``owner_down`` and ``pinned_quota_wall``.
  The same reason on later ticks adds nothing; a changed reason appends. At most :data:`MAX_PER_EPISODE` per
  episode, so two dispatchers that disagree cannot grow a card's history without bound.
* :func:`pinned_quota_hold` holds a fresh card (no run yet) on the ready lane whose explicit pin's provider pool
  is quota-walled right now (``kanban_provider_walls.live_quota_wall``): its first worker would exit 75 into
  the wall. Reason ``pinned_quota_wall``, ``until`` = the reset.
* :func:`latest` is the newest skip of the current ready episode; ``kanban_queue_position``, the chat-wait and
  the parent-unclaimed notices name it (:func:`words`) and drop the priority advice unless it is about ordering
  (:func:`is_ordering`). A card ready :data:`UNRECORDED_AFTER_SECONDS` with free slots and no recorded reason
  reads :data:`UNRECORDED` (:func:`unrecorded`), never a priority bump.

Advisory, like every notice: the dispatcher's live verdict decides admission, and a failure here never stops a
tick.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Optional

_log = logging.getLogger(__name__)

EVENT = "dispatch_skipped"
PINNED_QUOTA_WALL = "pinned_quota_wall"
MAX_PER_EPISODE = 16
DETAIL_CHARS = 200
UNRECORDED_AFTER_SECONDS = 30 * 60
UNRECORDED = "dispatcher skip reason unrecorded"
# Events after which an earlier skip no longer describes the card (same set the recorded guard reads).
_EPISODE_KINDS = ("continued", "scheduled", "reassigned", "reopened", "hold_released", "respawn_guarded")


def _episode_kinds() -> set[str]:
    from hermes_cli.kanban_diagnostics import _READY_WAIT_FLOOR_KINDS
    return {*_READY_WAIT_FLOOR_KINDS, *_EPISODE_KINDS}


def episode_skips(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """``dispatch_skipped`` payloads of the current ready episode, newest first. A ``respawn_guarded`` event
    ends the run of skips it follows: the guard is then the newer verdict (``kanban_queue_position``)."""
    from hermes_cli import kanban_db as kb
    kinds = sorted({*_episode_kinds(), EVENT})
    slots = ",".join("?" for _ in kinds)
    out: list[dict[str, Any]] = []
    for kind, payload, created_at in conn.execute(
            f"SELECT kind, payload, created_at FROM task_events WHERE task_id = ? AND kind IN ({slots}) "
            "ORDER BY created_at DESC, id DESC LIMIT ?", (task_id, *kinds, MAX_PER_EPISODE + 1)):
        if kind != EVENT:
            break
        out.append({**kb._json_dict(payload), "at": kb._to_epoch(created_at)})
    return out


def latest(conn: sqlite3.Connection, task_id: str) -> Optional[dict[str, Any]]:
    """The newest skip of the current ready episode (``reason``, ``detail``, ``until`` when known, ``at``)."""
    skips = episode_skips(conn, task_id)
    return skips[0] if skips and isinstance(skips[0].get("reason"), str) else None


def record(conn: sqlite3.Connection, task_id: str, reason: str, *, detail: Any = None,
           until: Optional[float] = None, extra: Optional[dict[str, Any]] = None) -> bool:
    """Append the skip unless the episode's latest skip already says the same; True when it appended."""
    from hermes_cli import kanban_db as kb
    detail = None if detail is None else str(detail)[:DETAIL_CHARS]
    with kb.write_txn(conn):
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["status"] not in ("ready", "review"):
            return False  # it left the lane this tick (no-progress park, claimed): nothing waits any more
        skips = episode_skips(conn, task_id)
        if skips and (skips[0].get("reason"), skips[0].get("detail")) == (reason, detail):
            return False
        if len(skips) >= MAX_PER_EPISODE:
            return False
        payload: dict[str, Any] = {"reason": reason, "detail": detail, **(extra or {})}
        if until is not None:
            payload["until"] = int(until)
        kb._append_event(conn, task_id, EVENT, payload)
    return True


def _tick_skips(result: Any) -> list[tuple[str, str, Any, Optional[float], dict[str, Any]]]:
    """``(task_id, reason, detail, until, extra)`` for every row ``result`` says the tick passed over."""
    out: list[tuple[str, str, Any, Optional[float], dict[str, Any]]] = []
    for name in ("skipped_ineligible", "capsule_refused", "no_progress_held", "claim_deferred"):
        out += [(tid, name, code, None, {}) for tid, code in getattr(result, name, ())]
    out += [(tid, "per_profile_capped", who, None, {"running": n})
            for tid, who, n in getattr(result, "skipped_per_profile_capped", ())]
    out += [(tid, "owner_down", f"{who}:{code}", None, {}) for tid, who, code in getattr(result, "owner_down", ())]
    out += [(tid, PINNED_QUOTA_WALL, f"{model} ({provider})", until, {"model": model, "provider": provider})
            for tid, model, provider, until in getattr(result, "pinned_quota_held", ())]
    return out


def record_tick(conn: sqlite3.Connection, result: Any, *, dry_run: bool = False) -> int:
    """Record every skip of one finished tick; returns how many events were appended. Never raises."""
    if dry_run or getattr(result, "skipped_locked", False) or getattr(result, "skipped_owned", False):
        return 0
    written = 0
    for tid, reason, detail, until, extra in _tick_skips(result):
        try:
            written += record(conn, tid, reason, detail=detail, until=until, extra=extra)
        except Exception:  # health: allow BLE001 -- the skip already happened; its evidence is advisory
            _log.debug("kanban dispatch: skip record failed for %s (%s)", tid, reason, exc_info=True)
    return written


# --- fresh card on a quota-walled explicit pin --------------------------------------------------------------


def pinned_quota_hold(conn: sqlite3.Connection, task_id: str) -> Optional[tuple[str, str, float]]:
    """``(model, provider, reset)`` when ``task_id`` has no run yet, an explicit concrete pin, and that pin's
    provider pool is quota-walled for the model right now; else None.

    Only a fresh card: once a run hit the wall its quota episode (``kanban_quota_episode``) and the in-class
    reroute (``kanban_pin_reroute``) decide. A fresh pinned card is routed straight to its pin
    (``kanban_model_tier.resolve_dispatch_tier``), so claiming it would only spawn a worker into the wall."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_pin_reroute import pin
    from hermes_cli.kanban_provider_walls import live_quota_wall
    if conn.execute("SELECT 1 FROM task_runs WHERE task_id = ? LIMIT 1", (task_id,)).fetchone() is not None:
        return None
    pinned = pin(kb.get_task(conn, task_id))
    if pinned is None:
        return None
    until = live_quota_wall(pinned[1], pinned[0])
    if until is None or until <= time.time():
        return None
    return pinned[0], pinned[1], float(until)


def hold_pinned_quota(conn: sqlite3.Connection, task_id: str, result: Any, *, lane: str) -> bool:
    """Pre-claim lane admission: True (recorded on ``result``) when :func:`pinned_quota_hold` holds the row."""
    if lane != "ready":
        return False
    held = pinned_quota_hold(conn, task_id)
    if held is None:
        return False
    result.pinned_quota_held.append((task_id, *held))
    result.clock_held[task_id] = (PINNED_QUOTA_WALL, int(held[2]))
    return True


# --- wording for queue places and notices ------------------------------------------------------------------


def _stamp(epoch: Any, now: Optional[float] = None) -> str:
    try:
        when = time.gmtime(int(epoch))
    except (TypeError, ValueError, OverflowError, OSError):
        return "an unknown time"
    today = time.gmtime(time.time() if now is None else now)
    return time.strftime("%H:%MZ" if when[:3] == today[:3] else "%Y-%m-%d %H:%MZ", when)


_WORDS = {
    PINNED_QUOTA_WALL: "held: pinned model {detail} quota-walled",
    "per_profile_capped": "skipped: assignee {detail} is at its per-profile worker cap",
    "skipped_ineligible": "skipped: this machine cannot lease it ({detail})",
    "capsule_refused": "skipped: capsule admission refused it ({detail})",
    "no_progress_held": "held: no-progress budget spent ({detail})",
    "claim_deferred": "skipped: the Board deferred its claim ({detail})",
    "owner_down": "held: the assignee's gateway service is down ({detail})",
}


def words(skip: dict[str, Any], *, now: Optional[float] = None) -> str:
    """The skip as one clause, e.g. ``held: pinned model claude-opus-5-5 (anthropic) quota-walled until 01:59Z``."""
    reason = str(skip.get("reason") or "unknown")
    detail = skip.get("detail") or "no detail"
    text = _WORDS.get(reason, "skipped: {reason} ({detail})").format(reason=reason, detail=detail)
    if skip.get("until"):
        text += f" until {_stamp(skip['until'], now)}"
    return text


def is_ordering(skip: Optional[dict[str, Any]]) -> bool:
    """True when the skip is about queue order or slots, where priority is a fair question."""
    return bool(skip) and skip.get("reason") == "claim_deferred" and skip.get("detail") == "capacity"


def advice(skip: dict[str, Any]) -> str:
    """What to do about a non-ordering skip; never a priority change."""
    if skip.get("reason") == PINNED_QUOTA_WALL:
        return ("it starts on its own when that wall resets. Re-pin it (`hermes kanban set-model <id> ...`) only if it "
                "cannot wait; priority changes do not clear a quota wall. Do not run its work outside the dispatcher")
    return ("resolve that dispatcher skip reason; priority changes do not clear it. "
            "Do not run its work outside the dispatcher")


UNRECORDED_ADVICE = (f"{UNRECORDED}: slots are free, yet no tick recorded why it passed this card over. Read the live "
                     "verdict (`hermes kanban dispatch --dry-run --json`) before changing anything; priority changes "
                     "are not the fix. Do not run its work outside the dispatcher")


def unrecorded(place: Optional[dict[str, Any]], ready_seconds: Optional[float]) -> bool:
    """A local-lane card ready :data:`UNRECORDED_AFTER_SECONDS` or more, with free slots, and no recorded skip."""
    place = place or {}
    if place.get("lane") != "local" or place.get("skip") or ready_seconds is None:
        return False
    running, limit = place.get("running"), place.get("limit")
    return (ready_seconds >= UNRECORDED_AFTER_SECONDS and isinstance(running, int) and isinstance(limit, int)
            and running < limit)


def notice_fields(place: Optional[dict[str, Any]], ready_seconds: Optional[float]) -> dict[str, Any]:
    """Payload fields for a ready-wait notice: ``dispatch_skip`` or ``skip_unrecorded``; empty otherwise."""
    skip = (place or {}).get("skip")
    if skip:
        return {"dispatch_skip": {k: skip.get(k) for k in ("reason", "detail", "until") if skip.get(k) is not None}}
    return {"skip_unrecorded": True} if unrecorded(place, ready_seconds) else {}


def notice_advice(payload: dict[str, Any]) -> Optional[str]:
    """The advice for a notice payload carrying :func:`notice_fields`, or None for the ordinary capacity advice."""
    skip = payload.get("dispatch_skip")
    if isinstance(skip, dict) and skip and not is_ordering(skip):
        return advice(skip)
    return UNRECORDED_ADVICE if payload.get("skip_unrecorded") else None
