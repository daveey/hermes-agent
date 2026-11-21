"""Hub admission at claim: the hub judges its own pre-gate cards before a slot runs them. [daveey]

The admission gate (first verdict on the hub 11-12 10:58Z) judges a card where it is created, moved or landed,
and a foreign card at pickup (``kanban_hub_admission_pickup``). Nothing judged a card the hub created before the
gate: its ``runs_on`` and ``placement_policy`` are NULL, so the claim backstop (``pickup.refusals``, foreign
hub-bound copies only) never saw it. On 11-12 the hourly pin sweep moved 7 such cards off the hub by hand after
their runs (all denied by the judge), and 26 more waited (t_01a1d09606b75027). The dispatcher is the one place
every hub run passes. The dispatcher freezes its ready/review candidates before :func:`screen`: async
wake probes finishing during admission must wait for the next tick, not join an unscreened claim pass.
The screen covers ready and review because a scheduled continuation can resume either lane:

* judges ``{title, body}`` outside any write transaction (one outage per tick: the rest are not judged);
* records a ``hub_admission`` event with ``entry: "claim"``;
* admit: claimed as before;
* deny: re-placed on the worker queue (the pickup deny route, never ``machine:worker-N``) with a ``moved`` event
  citing the verdict, so it is not claimed here;
* deny of a card that must stay (structural restriction, ``host_repo``, a hub worktree in the brief, an unlocated
  hub-local wake_check, or home-coordination gate): the verdict carries ``kept: <reason>``. A probe registered on
  this gateway stays here independently of the ready card's execution; unknown legacy probe custody stays local.
  A fleet probe in :data:`PORTABLE_WAKE_PROBES` (or ``kanban.placement.portable_wake_probes``) is also portable;
* an earlier deny is resolved immediately, including a card bounced back to this hub's machine pin: re-evaluate
  the structural/pickup constraints, record a current keep or move before claiming, without a new judge call;
  unchanged keep dispositions do not append duplicate events on each screen;
* judge unavailable: defer unjudged candidates, warning logged, nothing recorded (judged next tick).
  Previously admitted cards continue; a deny with no queue and no structural keep also defers.
* deferred (``Counts.deferred_ids``, left out of this tick's claims and judged next tick): a card edited while the
  judge ran (its verdict belongs to a stale snapshot, so none is recorded), and every card past
  :data:`MAX_JUDGED_PER_TICK` judge calls (each may take the judge's 30 s timeout; a ~27-card backlog at rollout
  would hold that tick's claims for minutes). Neither is ever claimed unjudged. The log line and the dispatch
  result (``DispatchResult.admission_deferred``) name them, so a ready card that keeps not starting is traceable.

A card with any verdict (forced or skipped included) makes no model call. :func:`unjudged_claims` is the
detector: this gateway's claims without a prior verdict or with a prior deny lacking an explicit keep/force.
Its gate counts audits riding a ``created`` payload as well as ``hub_admission`` events.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

_log = logging.getLogger(__name__)

ENTRY = "claim"

WINDOW_SECONDS = 7 * 86400
MAX_JUDGED_PER_TICK = 3  # judge calls per dispatcher tick; the rest wait, unclaimed, for the next tick
# A wake_check that reads hub-local state (Hermes home, a hub worktree) is probed where the card lives.
_HUB_PATH = r"(?:~|\$HOME|\$\{HOME\}|/home/[^/\s]+)/\.hermes/"
# Hub wake probes the fleet installs at the same ~/.hermes/bin path on every worker (state repo
# hosts/hermes-workers/install-core-tools.py HUB_PROBES); kanban.placement.portable_wake_probes overrides.
PORTABLE_WAKE_PROBES = ("alpha-hub-escalation-check.py",)


@dataclass
class Counts:
    judged: int = 0
    denied: int = 0
    moved: int = 0
    kept: int = 0
    home: int = 0  # of ``kept``: denies the claim gate would refuse on every worker (the home route)
    unavailable: int = 0
    deferred: int = 0
    repinned: int = 0  # earlier denied cards the self-check re-placed (kanban_worker_first_selfcheck.denied_target)
    deferred_ids: frozenset = field(default_factory=frozenset)  # not claimable this tick: judged next tick

    def defer(self, tid: str) -> None:
        self.deferred += 1
        self.deferred_ids = self.deferred_ids | {tid}

    def line(self) -> str:
        ids = f" ({', '.join(sorted(self.deferred_ids))})" if self.deferred_ids else ""
        return ("hub admission: claim screen judged={judged} denied={denied} moved={moved} kept={kept} "
                "home={home} unavailable={unavailable} deferred={deferred} repinned={repinned}"
                ).format(**asdict(self)) + ids


_SCREENABLE = "status IN ('ready', 'review') AND claim_lock IS NULL AND placement_policy IS NULL"


def _eligible(conn: sqlite3.Connection, card: sqlite3.Row) -> bool:
    """Unlabelled legacy cards, plus denied cards bounced back to this hub's machine pin."""
    from hermes_cli import kanban_hub_admission_pickup as pickup
    from hermes_cli import kanban_worker_node as wn
    labels = wn._parse(card["runs_on"])
    if not labels:
        return True
    if labels != [f"machine:{wn._own(conn)}"]:
        return False
    prior = pickup.verdict(conn, card["id"])
    return bool(prior and prior.get("admit") is False and not prior.get("forced"))


def _candidates(conn: sqlite3.Connection, lanes: Optional[list] = None) -> list[sqlite3.Row]:
    """Screenable cards in the claim pass's own dispatch order, so the per-tick judge budget covers the cards the
    claims reach first and never lets lower-priority work jump a deferred higher-priority card.

    That order is ONE ranking of ready and review together (``kanban_db_dispatch`` ranks its frozen lanes with
    :func:`kanban_dispatch_priority.order_with_remote_waiters`, the aged admission score), not the ready lane
    followed by the review lane: there, a review card the claim pass reaches first was deferred while lower ready
    cards spent the judge budget. ``lanes``: the dispatcher's frozen ready + review rows (review only when review
    dispatch is on); None reads both lanes now."""
    from hermes_cli.kanban_dispatch_priority import order_with_remote_waiters
    if lanes is None:
        from hermes_cli.kanban_admission_pools import lane_rows
        lanes = lane_rows(conn, "ready") + lane_rows(conn, "review")
    rows = {row["id"]: row for row in conn.execute(f"SELECT * FROM tasks WHERE {_SCREENABLE}").fetchall()}
    ordered = order_with_remote_waiters(conn, [row for row in lanes if row["id"] in rows])
    return [rows[row["id"]] for row in ordered if _eligible(conn, rows[row["id"]])]


def _current(conn: sqlite3.Connection, tid: str) -> tuple[Optional[sqlite3.Row], int]:
    """The card's row and event head, read together at the top of each judged card: up to
    :data:`MAX_JUDGED_PER_TICK` judge calls of 30 s each can run before a later card, so a row snapshot from the
    start of the tick would make :func:`_apply` defer it for any write in between and waste its judge call.
    The row is ``None`` when the card stopped being screenable meanwhile (claimed, labelled, no longer ready)."""
    event_head = _event_head(conn, tid)
    card = conn.execute(f"SELECT * FROM tasks WHERE id = ? AND {_SCREENABLE}", (tid,)).fetchone()
    return (card if card is not None and _eligible(conn, card) else None), event_head


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
    from hermes_cli.kanban_hub_probe_custody import registered_here
    raw_probe = _latest_wake_check(conn, card["id"])
    probe = _without_portable_probes(raw_probe, cfg)
    if probe and not registered_here(conn, card["id"], raw_probe) and any(
            re.search(p, probe, re.IGNORECASE) for p in (_HUB_PATH, kp.HOST_WORKTREE_PATH, kp.LOCAL_WORKTREE_PATH)):
        return "wake_check reads a hub-local path"
    return None


def _without_portable_probes(probe: str, cfg: dict) -> str:
    """``probe`` minus each ``~/.hermes/bin/<name>`` the fleet installs on every worker at the hub's path
    (``kanban.placement.portable_wake_probes``: basenames; default :data:`PORTABLE_WAKE_PROBES`). Such a probe runs
    wherever the card parks, delegating to its hub copy through hub-admin, so it does not tie the card to the hub
    (t_01a184cee94603eb was kept on the hub ~3 h by ``alpha-hub-escalation-check.py``). Any other hub path in the
    same command still keeps it."""
    names = (cfg.get("portable_wake_probes") if isinstance(cfg, dict) and "portable_wake_probes" in cfg
             else PORTABLE_WAKE_PROBES)
    if not probe or not isinstance(names, (list, tuple)):
        return probe
    for name in names:
        if isinstance(name, str) and name and "/" not in name:
            probe = re.sub(_HUB_PATH + r"bin/" + re.escape(name) + r"(?![\w.-])", " ", probe, flags=re.IGNORECASE)
    return probe


def _event_head(conn: sqlite3.Connection, tid: str) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]


def screen(conn: sqlite3.Connection, *, cfg: Optional[dict] = None, budget: int = MAX_JUDGED_PER_TICK,
           lanes: Optional[list] = None) -> Counts:
    """Judge this hub's ready, label-less, unjudged cards before the claim pass; see the module docstring.
    ``lanes``: the claim pass's frozen candidates (:func:`_candidates`). The caller must leave ``deferred_ids``
    out of this tick's claims."""
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
    calls = 0
    for tid in [row["id"] for row in _candidates(conn, lanes)]:
        card, event_head = _current(conn, tid)
        if card is None:
            continue
        if pickup.verdict(conn, tid) is not None:
            _self_check(conn, card, own, cfg, board, event_head, counts)
            continue
        if down:
            counts.unavailable += 1
            counts.defer(tid)
            continue
        if calls >= budget:
            counts.defer(tid)
            continue
        calls += 1
        fields = pickup.card_fields(card, board)
        try:
            found = judge.admit({"title": fields["title"], "body": fields["body"]})
        except Exception:  # health: allow BLE001 -- a failed judge is unavailable: defer unjudged candidates
            found = judge.Verdict()
        if not found.admit and found.why == judge.Verdict().why:
            down = True  # one outage per tick: never one judge timeout per card
            counts.unavailable += 1
            counts.defer(tid)
            _log.warning("hub admission: judge unavailable at claim for %s; deferring unjudged cards "
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
                # A deny pool_route keeps home (the claim gate refuses the text on every worker) names that reason.
                kept = audit.get("home")
                if not kept:
                    _apply(conn, own, tid, audit, None, expected=card, event_head=event_head)
                    counts.defer(tid)
                    continue  # no attested queue is not permission to run a portable deny on the hub
            if kept:
                audit["kept"] = kept
        applied = _apply(conn, own, tid, audit, target, expected=card, event_head=event_head)
        if applied:
            counts.moved += 1
        elif applied is None:
            counts.defer(tid)  # edited while judged: no verdict, so never claimed unjudged this tick
        elif "kept" in audit:
            counts.kept += 1
            counts.home += "home" in audit
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
        from hermes_cli.kanban_hub_admission_pickup import verdict
        if verdict(conn, tid) != audit:
            kb._append_event(conn, tid, "hub_admission", audit)
        if target is None:
            return False
        previous = wn._parse(row["runs_on"])  # None (NULL or '') or [] ('[]'): what the card carried
        wn.set_runs_on(conn, tid, target)
        kb._append_event(conn, tid, km.MOVED_EVENT, {
            "from_machine": own, "runs_on": target, "previous_runs_on": previous,
            "reason": f"hub admission denied at claim: {str(audit.get('why') or '')[:300]}",
            "move_no": len(km.history(conn, tid)) + 1, "forced": False, "matches": [], "queued": True,
            "hub_admission": audit})
    return True


def _self_check(conn: sqlite3.Connection, card: sqlite3.Row, own: str, cfg: dict, board: str, event_head: int,
                counts: Counts) -> None:
    """Resolve a prior deny now, not after a stale-ready interval that permits an intervening claim."""
    from hermes_cli import kanban_hub_admission_pickup as pickup
    try:
        prior = pickup.verdict(conn, card["id"])
        if not prior or prior.get("admit") is not False or prior.get("forced"):
            return
        fields = pickup.card_fields(card, board)
        audit = {k: v for k, v in prior.items() if k not in ("kept", "home")}
        audit["entry"] = ENTRY
        kept = keep_reason(conn, card, fields, cfg)
        target = None if kept else pickup._target(fields, own, cfg, audit)
        if target is not None:
            reason = f"hub admission denied before claim: {str(prior.get('why') or '')[:200]}"
            if prior.get("kept"):
                reason += f"; kept for '{prior['kept']}', which no longer holds"
            if not _replace(conn, own, card["id"], target, reason, expected=card, event_head=event_head):
                counts.defer(card["id"])
                return
            from hermes_cli.kanban_worker_first_selfcheck import log_repin
            log_repin(card["id"], target, reason)
            counts.repinned += 1
            return
        if not kept and not audit.get("home"):
            counts.defer(card["id"])
            return  # queue unavailability is not a structural permission to execute on the hub
        audit["kept"] = kept or audit["home"]
        applied = _apply(conn, own, card["id"], audit, None, expected=card, event_head=event_head)
        if applied is None:
            counts.defer(card["id"])
        else:
            counts.kept += 1
            counts.home += "home" in audit
    except Exception:  # health: allow BLE001 -- a denied card cannot run without a current disposition
        counts.defer(card["id"])
        _log.warning("hub admission: denied-card self-check failed for %s", card["id"], exc_info=True)


def _replace(conn: sqlite3.Connection, own: str, tid: str, target: list[str], reason: str,
             *, expected: sqlite3.Row, event_head: int) -> bool:
    """The self-check's re-placement of an earlier denied card (no new verdict): runs_on + a ``moved`` event."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_move as km
    from hermes_cli import kanban_worker_node as wn
    with kb.write_txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        if row is None or dict(row) != dict(expected) or _event_head(conn, tid) != event_head:
            return False  # changed since the check read it: the next tick decides again
        wn.set_runs_on(conn, tid, target)
        kb._append_event(conn, tid, km.MOVED_EVENT, {
            "from_machine": own, "runs_on": target, "previous_runs_on": wn._parse(row["runs_on"]),
            "reason": reason, "move_no": len(km.history(conn, tid)) + 1, "forced": False, "matches": [],
            "queued": True, "self_check": "hub_admission_denied"})
    return True


def print_deferred(ids: list[str]) -> None:
    """``hermes kanban dispatch`` text line for ``DispatchResult.admission_deferred``."""
    if ids:
        print(f"Deferred by the hub claim screen (unjudged, judged next tick): {', '.join(ids)}")


def screened_lanes(conn: sqlite3.Connection, dry_run: bool, review_enabled: bool, deferred=()) -> tuple:
    """Freeze claim candidates before judging. Async wakes during the screen wait for the next tick."""
    from hermes_cli.kanban_admission_pools import lane_rows
    from hermes_cli.kanban_db_sync import board_mode

    ready = lane_rows(conn, "ready")
    review = lane_rows(conn, "review") if review_enabled else []
    deferred = set(deferred)
    if not dry_run and board_mode(conn) == "primary":
        deferred.update(safe_screen(conn, ready + review))
    from hermes_cli.kanban_candidate_receipts import note
    for row in ready:
        if row["id"] in deferred:
            note(row["id"], "prefilter", "hub admission screen deferred", stage="screen")
    return ([row for row in ready if row["id"] not in deferred],
            [row for row in review if row["id"] not in deferred], deferred)


def safe_screen(conn: sqlite3.Connection, lanes: Optional[list] = None) -> frozenset:
    """Dispatcher seam: a failed screen cannot turn an unresolved deny into permission to claim."""
    try:
        return screen(conn, lanes=lanes).deferred_ids
    except Exception:  # health: allow BLE001 -- defer the frozen lanes instead of bypassing admission
        _log.warning("hub admission: claim screen failed; deferring ready/review claims", exc_info=True)
        return frozenset(row[0] for row in conn.execute(
            "SELECT id FROM tasks WHERE status IN ('ready', 'review')"))


def unjudged_claims(conn: sqlite3.Connection, own: str, *, now: Optional[float] = None,
                    window: int = WINDOW_SECONDS) -> list[tuple[str, int]]:
    """``[(task_id, claimed_at)]``: claims by ``own`` in the last ``window`` seconds, after the board's first
    verdict (the gate's rollout), without a verdict or with a deny lacking an explicit keep/force."""
    now = time.time() if now is None else now
    gate = conn.execute(  # a verdict is a hub_admission event or an audit riding a created payload
        "SELECT MIN(t) FROM (SELECT MIN(created_at) AS t FROM task_events WHERE kind = 'hub_admission' "
        "UNION ALL SELECT MIN(created_at) FROM task_events WHERE kind = 'created' AND json_valid(payload) "
        "AND json_type(payload, '$.hub_admission') = 'object')").fetchone()[0]
    if gate is None:
        return []
    rows = conn.execute(
        "SELECT e.task_id, e.created_at, e.id FROM task_events e WHERE e.kind = 'claimed' AND e.created_at >= ? "
        "AND json_valid(e.payload) AND json_extract(e.payload, '$.holder') = ? "
        "ORDER BY e.id", (max(int(gate), int(now) - window), own)).fetchall()
    out = []
    for task_id, created_at, _id in rows:
        prior = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND id < ? "
            "AND (kind = 'hub_admission' OR (kind = 'created' AND json_valid(payload) "
            "AND json_type(payload, '$.hub_admission') = 'object')) ORDER BY id DESC LIMIT 1",
            (task_id, _id)).fetchone()
        try:
            audit = json.loads(prior[1]) if prior and prior[1] else None
            if prior and prior[0] == "created":
                audit = audit.get("hub_admission") if isinstance(audit, dict) else None
        except (TypeError, ValueError):
            audit = None
        if (not isinstance(audit, dict)
                or (audit.get("admit") is False and not audit.get("kept") and not audit.get("forced"))):
            out.append((task_id, int(created_at)))
    return out
