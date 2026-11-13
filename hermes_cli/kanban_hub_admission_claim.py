"""Hub admission at claim: the hub judges its own pre-gate cards before a slot runs them. [daveey]

The admission gate (first verdict on the hub 11-12 10:58Z) judges a card where it is created, moved or landed,
and a foreign card at pickup (``kanban_hub_admission_pickup``). Nothing judged a card the hub created before the
gate: its ``runs_on`` and ``placement_policy`` are NULL, so the claim backstop (``pickup.refusals``, foreign
hub-bound copies only) never saw it. On 11-12 the hourly pin sweep moved 7 such cards off the hub by hand after
their runs (all denied by the judge), and 26 more waited (t_01a1d09606b75027). The dispatcher is the one place
every hub run passes, so before the hub claims a ready, label-less card with no verdict, :func:`screen`:

* judges ``{title, body}`` outside any write transaction (one outage per tick: the rest are not judged);
* records a ``hub_admission`` event with ``entry: "claim"``;
* admit: claimed as before;
* deny: re-placed on the worker queue (the pickup deny route, never ``machine:worker-N``) with a ``moved`` event
  citing the verdict, so it is not claimed here;
* deny of a card that must stay (structural restriction, ``host_repo``, a hub worktree in the brief, a hub-local
  wake_check, or no worker queue fits): the verdict carries ``kept: <reason>`` and the card is claimed as before;
* judge unavailable: claimed as before, warning logged, nothing recorded (judged at its next ready). Failing open
  here keeps a judge outage from freezing the hub; only pre-gate cards reach this path.

A card with any verdict (forced or skipped included) makes no model call. :func:`unjudged_claims` is the
detector: this gateway's claims of a card that had no verdict yet (``hermes doctor`` reports it on the hub).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

_log = logging.getLogger(__name__)

ENTRY = "claim"
NO_QUEUE = "no worker queue fits"
WINDOW_SECONDS = 7 * 86400
# A wake_check that reads hub-local state (Hermes home, a hub worktree) is probed where the card lives.
_HUB_PATH = r"(?:~|\$HOME|\$\{HOME\}|/home/[^/\s]+)/\.hermes/"


@dataclass
class Counts:
    judged: int = 0
    denied: int = 0
    moved: int = 0
    kept: int = 0
    unavailable: int = 0

    def line(self) -> str:
        return ("hub admission: claim screen judged={judged} denied={denied} moved={moved} kept={kept} "
                "unavailable={unavailable}").format(**asdict(self))


def _candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tasks WHERE status = 'ready' AND claim_lock IS NULL "
        "AND (runs_on IS NULL OR runs_on IN ('', '[]')) AND placement_policy IS NULL ORDER BY id").fetchall()


def _latest_wake_check(conn: sqlite3.Connection, task_id: str) -> str:
    """The newest wake_check the card's events carry (``scheduled``, ``condition_met``, ``continued``, ...)."""
    row = conn.execute(
        "SELECT json_extract(payload, '$.wake_check') FROM task_events WHERE task_id = ? AND json_valid(payload) "
        "AND json_type(payload, '$.wake_check') = 'text' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return str(row[0]) if row and row[0] else ""


def keep_reason(conn: sqlite3.Connection, card: sqlite3.Row, fields: dict, cfg: dict) -> Optional[str]:
    """Why a denied card must still run here, or None when the worker queue can take it."""
    from hermes_cli import kanban_placement as kp
    structural = kp.structural_reason({k: fields[k] for k in ("title", "body", "workspace_kind", "assignee",
                                                              "requires", "board")}, cfg)
    if structural:
        return structural
    if "host_repo" in (fields.get("requires") or []):
        return "requires host_repo"
    text = f"{fields['title']}\n{fields['body']}"
    if re.search(kp.HOST_WORKTREE_PATH, text, re.IGNORECASE) or re.search(kp.LOCAL_WORKTREE_PATH, text,
                                                                           re.IGNORECASE):
        return "hub worktree path in the brief"
    probe = _latest_wake_check(conn, card["id"])
    if probe and any(re.search(p, probe, re.IGNORECASE) for p in (_HUB_PATH, kp.HOST_WORKTREE_PATH,
                                                                   kp.LOCAL_WORKTREE_PATH)):
        return "wake_check reads a hub-local path"
    return None


def _event_head(conn: sqlite3.Connection, tid: str) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]


def screen(conn: sqlite3.Connection, *, cfg: Optional[dict] = None) -> Counts:
    """Judge this hub's ready, label-less, unjudged cards before the claim pass; see the module docstring."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_hub_admission as judge
    from hermes_cli import kanban_hub_admission_pickup as pickup
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli.kanban_hub_admission_placement import is_hub
    counts = Counts()
    cfg = kp.config() if cfg is None else cfg
    own = wn._own(conn)
    if not is_hub(own, cfg) or conn.in_transaction:
        return counts
    board = sync.board_slug_for_path(sync._db_key(conn) or "")
    down = False
    for card in _candidates(conn):
        tid = card["id"]
        event_head = _event_head(conn, tid)
        if pickup.verdict(conn, tid) is not None:
            continue
        if down:
            counts.unavailable += 1
            continue
        fields = pickup._fields(card, board)
        try:
            found = judge.admit({"title": fields["title"], "body": fields["body"]})
        except Exception:  # health: allow BLE001 -- a failed judge is unavailable: claimed as before, unjudged
            found = judge.Verdict()
        if not found.admit and found.why == judge.Verdict().why:
            down = True  # one outage per tick: never one judge timeout per card
            counts.unavailable += 1
            _log.warning("hub admission: judge unavailable at claim for %s; claiming pre-gate cards unjudged "
                         "this tick", tid)
            continue
        counts.judged += 1
        audit: dict[str, Any] = {**asdict(found), "entry": ENTRY, "runs_on": None, "created_on": card["created_on"]}
        target = None
        if not found.admit:
            counts.denied += 1
            kept = keep_reason(conn, card, fields, cfg)
            target = None if kept else pickup._target(fields, own, cfg, audit)
            if kept is None and target is None:
                kept = NO_QUEUE
            if kept:
                audit["kept"] = kept
        applied = _apply(conn, own, tid, audit, target, expected=card, event_head=event_head)
        if applied:
            counts.moved += 1
        elif applied is False and "kept" in audit:
            counts.kept += 1
    if counts != Counts():
        _log.info(counts.line())
    return counts


def _apply(conn: sqlite3.Connection, own: str, tid: str, audit: dict, target: Optional[list[str]],
           *, expected: sqlite3.Row, event_head: int) -> Optional[bool]:
    """Record the verdict; move a denied card to ``target``. True when moved. Skips a card that changed meanwhile."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_move as km
    from hermes_cli import kanban_worker_node as wn
    with kb.write_txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        if row is None or dict(row) != dict(expected) or _event_head(conn, tid) != event_head:
            return None  # the verdict and route belong only to the snapshot the judge saw
        kb._append_event(conn, tid, "hub_admission", audit)
        if target is None:
            return False
        wn.set_runs_on(conn, tid, target)
        kb._append_event(conn, tid, km.MOVED_EVENT, {
            "from_machine": own, "runs_on": target, "previous_runs_on": None,
            "reason": f"hub admission denied at claim: {str(audit.get('why') or '')[:300]}",
            "move_no": len(km.history(conn, tid)) + 1, "forced": False, "matches": [], "queued": True,
            "hub_admission": audit})
    return True


def safe_screen(conn: sqlite3.Connection) -> None:
    """Dispatcher seam: a failed screen never stops the tick (the cards are claimed as before)."""
    try:
        screen(conn)
    except Exception:  # health: allow BLE001 -- claim-time admission is a backstop; local dispatch continues
        _log.warning("hub admission: claim screen failed", exc_info=True)


def unjudged_claims(conn: sqlite3.Connection, own: str, *, now: Optional[float] = None,
                    window: int = WINDOW_SECONDS) -> list[tuple[str, int]]:
    """``[(task_id, claimed_at)]``: claims by ``own`` in the last ``window`` seconds, after the board's first
    verdict (the gate's rollout), of a card with no verdict before that claim."""
    now = time.time() if now is None else now
    gate = conn.execute("SELECT MIN(created_at) FROM task_events WHERE kind = 'hub_admission'").fetchone()[0]
    if gate is None:
        return []
    rows = conn.execute(
        "SELECT e.task_id, e.created_at, e.id FROM task_events e WHERE e.kind = 'claimed' AND e.created_at >= ? "
        "AND json_valid(e.payload) AND json_extract(e.payload, '$.holder') = ? "
        "AND NOT EXISTS (SELECT 1 FROM task_events v WHERE v.task_id = e.task_id AND v.kind = 'hub_admission' "
        "AND v.id < e.id) ORDER BY e.id", (max(int(gate), int(now) - window), own)).fetchall()
    out = []
    for task_id, created_at, _id in rows:
        created = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
                               "ORDER BY id LIMIT 1", (task_id,)).fetchone()
        try:
            audit = (json.loads(created[0]) or {}).get("hub_admission") if created and created[0] else None
        except (TypeError, ValueError, AttributeError):
            audit = None
        if not isinstance(audit, dict):
            out.append((task_id, int(created_at)))
    return out
