"""Default-placement cards stranded on their held creator: one pickup-stall alarm per incident. [daveey]

t_01a1d4f2f591d595 (2026-11-13): t_01a1d49d50e4c656 (the PR #2168 land-lane owner) woke ``ready`` on worker-6 at
12:32Z with ``runs_on`` NULL (default placement: "its creator"). worker-6 was on a hub-worker-core-rollout hold (drain
only, up to its 90 min hard max). Every other pool worker runs ``kanban.sync.pickup: explicit``, which copies only
cards with ``runs_on`` (``kanban_worker_node._skip_reason``), so no machine could claim it. It sat 14 min with a
land-lane ticket held behind it, until an operator moved it to the pool by hand. The pickup-stall detector
(:mod:`hermes_cli.kanban_pickup_stall`) watched only foreign cards pinned ``machine:<this host>``, so nothing alerted.

This covers the creator's side. On every dispatcher sweep of a held machine M, a card on M's own board that is
``ready``, unclaimed, default placement (``runs_on`` NULL), created on M, with a ``scratch`` workspace, is stranded when
no other machine would copy it: no active, unheld, live (not draining, recently seen) machine with non-explicit
pickup passes :func:`hermes_cli.kanban_worker_node.eligible` for it. A card held by a dispatcher respawn guard does
not count (it would wait anyway). When such a card has waited past ``kanban.sync.pickup_stall_seconds`` (default
600 s) since both its ``ready_since`` and the hold's start, ONE host-wide alarm (key ``pickup-stall:<M>:held-default:``)
lists every such card, placed on another machine by :func:`hermes_cli.kanban_pickup_stall.alarm_placement`. Its run
may re-place exactly the listed cards to the pool queue (:mod:`hermes_cli.kanban_pickup_stall_repin`). A rollout-hold
card should already have been moved by hub-worker-core-rollout within one 5-minute pass, so the alarm is the backstop
when that move did not happen. A ``dir``/``worktree`` card runs only where its workspace is, so it is never listed: no
move can help it.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

_log = logging.getLogger(__name__)

SLUG = "held-default"
_BOUND = ("dir", "worktree")


def others_take_default(card: Mapping[str, Any], own: str, machines: Mapping[str, Mapping[str, Any]], *,
                        now_ms: int) -> list[str]:
    """Machines other than ``own`` whose pickup would copy default-placement ``card`` now: active, pickup on and
    not ``explicit`` (explicit copies only cards with ``runs_on``), live (not draining, ``last_seen`` fresh:
    :func:`kanban_pickup_stall._live`, as :func:`kanban_pickup_stall.alarm_placement` checks) and
    :func:`kanban_worker_node.eligible`. A stale or draining machine claims nothing, so counting it would silence
    the alarm for a card no one can take. A full machine still counts: it copies the card and runs it when a slot
    frees."""
    from hermes_cli import kanban_pickup_stall as kps
    from hermes_cli import kanban_worker_node as wn
    out = []
    for name, entry in machines.items():
        if name == own or not entry or entry.get("state", "active") != "active" or not wn.advertises_pickup(entry):
            continue
        if not kps._live(entry, now_ms):
            continue
        if str(entry.get("pickup")).strip().lower() == "explicit":
            continue
        if wn.eligible(card, name, machines) is None:
            out.append(name)
    return sorted(out)


def _hold_ms(entry: Mapping[str, Any]) -> int:
    at = entry.get("hold_at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or at <= 0:
        return 0  # unknown start: the card's own ready clock decides
    return int(at if at > 1e11 else at * 1000)


def stranded(conn, own: str, machines: Optional[Mapping[str, Mapping[str, Any]]], *,
             now_ms: int, stall_s: int) -> list[dict[str, Any]]:
    """Cards on ``own``'s board that only ``own`` may run while ``own`` is held, waiting past ``stall_s``; oldest
    first. Empty when ``own`` is not held or the registry is unreadable."""
    from hermes_cli import kanban_pickup_held as kph
    from hermes_cli import kanban_pickup_stall as kps
    entry = (machines or {}).get(own)
    if machines is None or not entry or not entry.get("hold"):
        return []
    held_ms = _hold_ms(entry)
    out = []
    rows = conn.execute("SELECT * FROM tasks WHERE status = 'ready' AND runs_on IS NULL AND claim_lock IS NULL "
                        "AND (created_on IS NULL OR created_on = ?)", (own,)).fetchall()
    for row in rows:
        if row["workspace_kind"] in _BOUND:
            continue
        keys = row.keys()
        ready = row["ready_since"] if "ready_since" in keys and row["ready_since"] is not None \
            else int(row["created_at"] or 0) * 1000
        since = max(int(ready), held_ms)
        if now_ms - since <= stall_s * 1000:
            continue
        card = {**dict(row), "created_on": row["created_on"] or own}
        if others_take_default(card, own, machines, now_ms=now_ms):
            continue
        if kph.board_hold(conn, row) is not None:
            continue  # a respawn guard holds it: it would wait on any machine
        out.append({"id": row["id"], "title": row["title"], "tenant": row["tenant"], "since_ms": since,
                    "skip": kps.latest(conn, row["id"])})
    out.sort(key=lambda c: (c["since_ms"], c["id"]))
    return out


def file_alarm(conn, own: str, cards: list[dict[str, Any]], *, now_ms: int, stall_s: int,
               entry: Mapping[str, Any]) -> str:
    """ONE alarm per held machine listing ``cards`` (an open one is reused and gets new targets appended)."""
    from hermes_cli import kanban_pickup_stall as kps
    from hermes_cli import kanban_pickup_stall_repin as repin
    from hermes_cli.kanban_model_neutral import forwarded_text
    oldest = cards[0]
    listed = "\n".join(forwarded_text(f"- {c['id']} ({c['title']}), ready {kps._minutes(c['since_ms'], now_ms)} min, default "
                       "placement, created here") for c in cards[:20])
    who = entry.get("hold_owner") or entry.get("hold_by") or "?"
    body = (f"{len(cards)} default-placement card(s) created on {own} have been ready past {stall_s} s while {own} "
            f"is held in the machine registry (by {who}: {entry.get('hold_reason')!r}). Default placement means "
            f"\"its creator\", and no other machine copies them: every other pickup machine is held, `explicit` "
            "(copies only cards with runs_on) or lacks the creator's labels. Nothing claims them until the hold "
            f"ends. Re-place each listed card to the pool queue; do not lift or shorten the hold.\n\n"
            f"Oldest: {oldest['id']}\nHistorical source title:\n{forwarded_text(oldest['title'])}\n\n"
            f"{kps._registry_line(own)}\n\nStranded cards on {own}:\n\n{listed}")
    tid = kps._create(conn, key=f"{kps.KEY_PREFIX}{own}:{SLUG}:", tenant=oldest["tenant"], own=own, now_ms=now_ms,
                      title=(f"Pickup stranded on held {own}: oldest {oldest['id']} ready "
                             f"{kps._minutes(oldest['since_ms'], now_ms)} min ({len(cards)} default-placement "
                             "card(s) only it may run)"),
                      body=body, howto=repin.held_default_howto(own))
    kps._refresh_targets(conn, tid, cards, now_ms)
    return tid


def sweep(conn, own: str, *, now_ms: int) -> list[str]:
    """The held-creator pass of :func:`kanban_pickup_stall.sweep`: the alarm id, or nothing."""
    from hermes_cli import kanban_pickup_stall as kps
    from hermes_cli import kanban_worker_node as wn
    reg = wn.registry()
    machines = None if reg is None else (reg.get("machines") or {})
    stall_s = kps.stall_seconds()
    cards = stranded(conn, own, machines, now_ms=now_ms, stall_s=stall_s)
    if not cards:
        return []
    _log.info("pickup-stall: %d default-placement card(s) stranded on held %s: %s", len(cards), own,
              [c["id"] for c in cards])
    return [file_alarm(conn, own, cards, now_ms=now_ms, stall_s=stall_s, entry=(machines or {})[own])]
