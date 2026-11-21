"""Dispatcher sweep: a READY card no enrolled machine can ever claim is flagged at once, not after hours. [daveey]

Incident t_01a1f867bac2344e (2026-11-20): ``runs_on=["macos"]`` on the hub; the only macos machines are
``pinned_only`` (``kanban_claimants``). It sat ready 24 h, and the one notice that fired said "waiting for macbook,
mini", so its owner kept expecting a Mac. This sweep runs each dispatcher tick, covers every way a card gets into
that state (create before the check existed, edit, move, a worker-scaler rehome onto the hub), and:

* posts ONE ``chat_wait_unclaimed`` notice per (card, labels) to the creator's chats, naming each candidate machine's
  reason and the cross-gateway create remedy (``kanban_chat_wait.notice_text``);
* on the hub (the operator lane), keeps ONE agent-owned alarm card open for the class: a new card is added to it
  by comment (``kanban_pickup_stall._create``), never a second alarm.

Temporary states (hold, drain, quota, offline) never count: ``kanban_claimants`` reads structure only.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

_log = logging.getLogger(__name__)

KEY = "unclaimable-ready:"
REFIRE_S = 6 * 3600  # a closed alarm is not reopened sooner than this while its cards stay stranded
HOWTO = (
    "Each listed card is ready but no enrolled machine can ever claim it, so waiting will not start it. For each: "
    "if its work needs the named machine, create a new card on that machine's own gateway (cross-gateway create, "
    "placement='<machine>') and archive the stranded one with a comment naming the replacement; otherwise "
    "`kanban_move` it to labels an active pool worker carries. Do not change Mac policy (pinned_only) to make it "
    "claimable. Comment on this alarm with what you did for each card.")


def stranded(conn, machines, me: str) -> list[dict[str, Any]]:
    """Ready, unclaimed cards of this board whose stored ``runs_on`` no machine could ever claim."""
    from hermes_cli import kanban_claimants as kc
    from hermes_cli import kanban_runs_on_capabilities as roc
    from hermes_cli import kanban_worker_node as wn
    out = []
    for row in conn.execute("SELECT id, title, tenant, runs_on, created_on, created_at FROM tasks WHERE status = 'ready' "
                            "AND runs_on IS NOT NULL AND claim_lock IS NULL AND lease_holder IS NULL "
                            "AND current_run_id IS NULL"):
        labels = wn._parse(row["runs_on"])
        home = row["created_on"] or me
        if labels and roc.impossible(labels, machines, me, home):
            out.append({"id": row["id"], "title": row["title"], "tenant": row["tenant"], "runs_on": labels,
                        "created_at": row["created_at"], "why": kc.refusals(labels, machines, home)})
    return out


def _notify(conn, card: dict[str, Any], now: float) -> bool:
    from hermes_cli import kanban_claimants as kc
    from hermes_cli import kanban_parent_unclaimed as pu
    from hermes_cli.kanban_chat_wait import NOTICE_KIND
    waited = max(0.0, now - pu.ready_since(conn, card["id"], card["created_at"]))
    return pu.post_notice(
        conn, card["id"], card["id"], wait_key="unclaimable:" + json.dumps(card["runs_on"]),
        waited_seconds=waited, ready_seconds=waited, parent_title=card["title"], kind=NOTICE_KIND,
        extra={"unclaimable": kc.stranded_text(card["id"], card["runs_on"], card["why"])})


def _alarm(conn, cards: list[dict[str, Any]], own: str, now_ms: int) -> Optional[str]:
    from hermes_cli import kanban_claimants as kc
    from hermes_cli import kanban_pickup_stall as kps
    key = f"{kps.KEY_PREFIX}{own}:{KEY}"
    existing = kps._covering_repair(conn, key, now_ms)
    if existing:
        body = (conn.execute("SELECT body FROM tasks WHERE id = ?", (existing,)).fetchone()[0] or "")
        seen = {r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (existing,))}
        if all(c["id"] in body or any(c["id"] in s for s in seen if s) for c in cards):
            return existing  # every card is already on the open alarm: one alarm, no repeat comments
    since = int(now_ms / 1000) - REFIRE_S
    if not existing and conn.execute(
            "SELECT 1 FROM tasks WHERE created_by = ? AND idempotency_key >= ? AND idempotency_key < ? "
            "AND created_at > ? LIMIT 1", (kps.DETECTOR, key, key + "\U0010ffff", since)).fetchone():
        return None  # an alarm for this class closed recently: no immediate re-fire loop
    listed = "\n".join(f"- {c['id']} ({c['title']}): " + kc.stranded_text(c["id"], c["runs_on"], c["why"])
                       for c in cards)
    body = (f"{len(cards)} ready card(s) on {own} that no enrolled machine can ever claim (structural, not a wait):"
            f"\n\n{listed}\n\n{HOWTO}")
    return kps._create(conn, key=key, title=f"Ready cards no machine can claim on {own} ({len(cards)})", body=body,
                       tenant=cards[0]["tenant"], own=own, now_ms=now_ms, howto=HOWTO)


def sweep(conn, *, now: Optional[float] = None) -> list[str]:
    """Notify each stranded ready card's chats once and keep the hub's single alarm current; returns card ids."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_pickup_stall as kps
    from hermes_cli import kanban_worker_node as wn
    if sync.board_mode(conn) != "primary":
        return []
    try:
        reg = wn.registry()
    except Exception:  # health: allow BLE001 -- unknown registry is never evidence of impossibility
        return []
    machines = (reg or {}).get("machines") or None
    me = wn._own(conn)
    if not machines or not me:
        return []
    now = time.time() if now is None else now
    cards = stranded(conn, machines, me)
    if not cards:
        return []
    for card in cards:
        try:
            _notify(conn, card, now)
        except Exception:  # health: allow BLE001 -- one card's notice never blocks the alarm
            _log.warning("kanban unclaimable notice failed for %s", card["id"], exc_info=True)
    if me == kps.OPERATOR_LANE:
        _alarm(conn, cards, me, int(now * 1000))
    return [c["id"] for c in cards]


def safe_sweep(conn) -> list[str]:
    try:
        return sweep(conn)
    except Exception:  # health: allow BLE001 -- the detector must never stop dispatch
        _log.warning("kanban dispatcher: unclaimable-ready sweep failed", exc_info=True)
        return []
