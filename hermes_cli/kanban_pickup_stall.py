"""Pickup evidence and the stalled-pickup detector for worker nodes (``kanban.sync.pickup``). [daveey]

t_01a1c9097f6a94bc (2026-11-11): t_01a1c81a5323b6d1 was ready on the Board, pinned ``runs_on=[machine:hub]``, for
23 min while the hub neither imported nor claimed it. Its child waited, nothing alerted, and the cause could not be
read back: pickup kept no record of why a pass skipped a candidate.

* Evidence: every pickup pass (and every dispatcher tick that has no spawn budget, where pickup does not run) records
  for each candidate pinned ``machine:<this host>`` that it did not import, ONE row per (card, reason) in the
  machine-local ``kanban_pickup_skips`` table (not synced; first/last seen, pass count, detail). Rows are rewritten at
  most every :data:`REFRESH_SECONDS` unless the reason changed, pruned after :data:`RETENTION_SECONDS` and capped at
  :data:`MAX_ROWS`. ``kanban_show`` of the card on this host returns the latest as ``pickup_skip``.
* Detector (:func:`sweep`, every non-dry dispatcher tick before the spawn cap): a card ``ready`` in this host's full
  replica, pinned ``machine:<this host>``, created elsewhere, with no live lease and not imported here, whose
  ``ready_since`` and newest move onto this host are older than ``kanban.sync.pickup_stall_seconds`` (default 600)
  gets ONE agent repair card filed
  by this host. An open incident is reused and repeated evidence is appended; a closed incident is replaced. The repair is
  never pinned here (:func:`alarm_placement`): this host is the one taking no Board work, so the repair is pinned
  to another active, unheld machine that can run it now, or the hub operator lane, and an open unclaimed repair
  whose pin is stranded is re-pinned. Its body carries this host's machine-local evidence (pickup skips, registry
  entry, replica path). Host-wide causes share
  ONE card per host naming the oldest card: ``budget_exhausted`` (the queue is simply full), ``hold`` and an
  unreadable machine registry. A ``hold`` repair needs the hold in the live registry entry the pass reads (a
  lifted hold files nothing; the card's next pickup skip classifies it), and every pass closes an open host-wide
  repair whose cards have all left ``ready`` (or, for ``hold``, whose hold lifted):
  :mod:`hermes_cli.kanban_pickup_stall_resolve`. A ``hold`` that states ``release by HH:MMZ`` (or is the core
  rollout's own,
  :func:`expected_hold`) files nothing until that release plus the stall threshold has passed; nor does a card's own
  ``quota_hold`` (local dispatch would hold it on its quota wall) before its stated deadline. Missing evidence gets
  one additional stall interval of durable grace, then one
  host alert (not a fabricated refusal); a failed pickup must not silence the detector forever.
  ``budget_exhausted`` is recorded only for a card a slot would have started, so a
  refusal is never hidden behind a full queue. No Inbox post, no human ask.
"""
from __future__ import annotations

import logging
import math
import re
import time
from typing import Any, Mapping, Optional

_log = logging.getLogger(__name__)

TABLE = "kanban_pickup_skips"
REASONS = ("budget_exhausted", "profile_ineligible", "capability_missing", "parents_incomplete", "hold", "other",
           "evidence_missing", "quota_hold", "guard_held")
# ``hold`` is this machine's registry hold (host-wide, an operator's); ``guard_held`` is one card the Board shows held
# by a dispatcher respawn guard (active_pr, subscription_quota_reset, ...), which lifts when that guard does.
DEFAULT_STALL_SECONDS = 600
REFRESH_SECONDS = 60
RETENTION_SECONDS = 7 * 86400
MAX_ROWS = 2000
REPLACE_AFTER_SECONDS = 3600
DETECTOR = "pickup-stall-detector"
KEY_PREFIX = "pickup-stall:"
QUEUE_FULL = "queue-full"


def _now_ms(now: Optional[float] = None) -> int:
    return int((time.time() if now is None else now) * 1000)


def _has_table(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)).fetchone() is not None


def record(conn, skips: Mapping[str, tuple[str, Optional[str]]], *, now: Optional[float] = None) -> int:
    """Record ``{task_id: (reason, detail)}`` from one pass; returns rows written. A (card, reason) already recorded
    is refreshed only when it is not the card's latest reason or :data:`REFRESH_SECONDS` passed, so a busy tick loop
    writes nothing most ticks."""
    if not skips:
        return 0
    from hermes_cli.kanban_db_connect import write_txn
    at = _now_ms(now)
    have: dict[str, dict[str, int]] = {}
    if _has_table(conn):
        marks = ",".join("?" * len(skips))
        for row in conn.execute(f"SELECT task_id, reason, last_at FROM {TABLE} WHERE task_id IN ({marks})",
                                tuple(skips)):
            have.setdefault(row[0], {})[row[1]] = int(row[2])
    todo = []
    for tid, (reason, detail) in skips.items():
        reason = reason if reason in REASONS else "other"
        mine = have.get(tid, {})
        newest = max(mine, key=lambda r: mine[r]) if mine else None
        if newest == reason and at - mine[reason] < REFRESH_SECONDS * 1000:
            continue
        todo.append((tid, reason, str(detail)[:200] if detail else None))
    if not todo:
        return 0
    with write_txn(conn):
        conn.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (task_id TEXT NOT NULL, reason TEXT NOT NULL, "
                     "detail TEXT, first_at INTEGER NOT NULL, last_at INTEGER NOT NULL, "
                     "passes INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (task_id, reason))")
        for tid, reason, detail in todo:
            # Strictly after the card's other rows: a reason recorded in the same millisecond is still the latest.
            ms = max(at, int(conn.execute(f"SELECT COALESCE(MAX(last_at), 0) + 1 FROM {TABLE} WHERE task_id = ? "
                                          "AND reason != ?", (tid, reason)).fetchone()[0]))
            conn.execute(
                f"INSERT INTO {TABLE} (task_id, reason, detail, first_at, last_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (task_id, reason) DO UPDATE SET detail = excluded.detail, "
                "last_at = excluded.last_at, passes = passes + 1", (tid, reason, detail, ms, ms))
    return len(todo)


def latest(conn, task_id: str) -> Optional[dict[str, Any]]:
    """This host's latest recorded pickup skip for ``task_id`` (``reason``, ``detail``, ``first_at``/``last_at`` ms,
    ``passes``), or None."""
    if not _has_table(conn):
        return None
    row = conn.execute(f"SELECT reason, detail, first_at, last_at, passes FROM {TABLE} WHERE task_id = ? "
                       "ORDER BY last_at DESC LIMIT 1", (task_id,)).fetchone()
    return None if row is None else dict(zip(("reason", "detail", "first_at", "last_at", "passes"), tuple(row)))


def _stamp(ms: int) -> str:
    from datetime import UTC, datetime
    return datetime.fromtimestamp(int(ms) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def describe(skip: Optional[Mapping[str, Any]]) -> str:
    if not skip:
        return "none recorded (no pickup pass on this host evaluated the card)"
    detail = f" ({skip['detail']})" if skip.get("detail") else ""
    return (f"{skip['reason']}{detail}, first {_stamp(skip['first_at'])}, last {_stamp(skip['last_at'])}, "
            f"{skip['passes']} pass(es)")


def not_here_note(conn, task_id: str) -> str:
    """Suffix for ``hermes kanban show``'s not-found line when pickup skipped the card on this host."""
    try:
        skip = latest(conn, task_id)
    except Exception:  # health: allow BLE001 -- an advisory note must never fail the read
        return ""
    return f" (not imported here; last pickup skip: {describe(skip)})" if skip else ""


def _prune(conn, now_ms: int) -> None:
    if not _has_table(conn):
        return
    floor = now_ms - RETENTION_SECONDS * 1000
    count = conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
    stale = conn.execute(f"SELECT 1 FROM {TABLE} WHERE last_at < ? LIMIT 1", (floor,)).fetchone()
    if not stale and count <= MAX_ROWS:
        return
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        conn.execute(f"DELETE FROM {TABLE} WHERE last_at < ?", (floor,))
        conn.execute(f"DELETE FROM {TABLE} WHERE rowid NOT IN (SELECT rowid FROM {TABLE} "
                     "ORDER BY last_at DESC LIMIT ?)", (MAX_ROWS,))


def stall_seconds() -> int:
    """``kanban.sync.pickup_stall_seconds`` (default :data:`DEFAULT_STALL_SECONDS`)."""
    from hermes_cli import kanban_db_sync as sync
    value = sync.sync_config().get("pickup_stall_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return DEFAULT_STALL_SECONDS
    return int(value)


# Events that rewrite a card's ``runs_on`` and carry the new labels as ``payload.runs_on``.
_PIN_EVENTS = ("moved", "placement_repinned", "pool_pinned", "pool_unpinned")


def _pinned_here_since_ms(replica, task_id: str, own: str) -> Optional[int]:
    """When the newest placement event naming ``machine:<own>`` was recorded (ms), or None.

    t_01a1cea82846df08 (2026-11-12): a card ready on the hub for 10 min was moved to worker-6 and worker-6 raised
    "waiting 10 min" 9 s later. Time pinned elsewhere is not this machine's stall."""
    marks = ",".join("?" * len(_PIN_EVENTS))
    row = replica.execute(
        f"SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind IN ({marks}) AND json_valid(payload) "
        "AND EXISTS (SELECT 1 FROM json_each(task_events.payload, '$.runs_on') WHERE value = ?)",
        (task_id, *_PIN_EVENTS, f"machine:{own}")).fetchone()
    return None if row is None or row[0] is None else int(row[0]) * 1000


# The origin's worker-first placement observation (``kanban_queue_reason.EVENT``); ``lifecycle.runs_on`` is the
# card's ``runs_on`` (JSON text, or null) on the origin when the Board answered.
_QUEUE_EVENT = "worker_queue_reason"


def _seen_unpinned_here_ms(replica, task_id: str, own: str) -> Optional[int]:
    """When the origin's newest worker-placement observation saw the card NOT pinned ``machine:<own>`` (ms), or None.

    t_01a1cfdfb6f3f7ea (2026-11-12): a worker-first card's Board reservation pins it to a worker for seconds and
    lapses (``runs_on`` null again), with no placement event. t_01a1cfa91548dad7 was reserved to worker-2 at
    12:49:27Z, unpinned by 12:50:35Z and reserved again; worker-2 counted from ``ready_since`` (12:09Z) and filed
    "waiting 40 min" at 12:50:44Z. A pin lasts at most since the last time it was seen absent."""
    row = replica.execute(
        "SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind = ? AND json_valid(payload) "
        "AND NOT EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(json_extract(task_events.payload, "
        "'$.lifecycle.runs_on')) THEN json_extract(task_events.payload, '$.lifecycle.runs_on') ELSE '[]' END) "
        "WHERE value = ?)", (task_id, _QUEUE_EVENT, f"machine:{own}")).fetchone()
    return None if row is None or row[0] is None else int(row[0]) * 1000


def stalled(conn, replica, own: str, *, now: Optional[float] = None) -> list[dict[str, Any]]:
    """Cards ready in ``replica``, pinned ``machine:<own>``, created elsewhere, unleased, not in the live file,
    ready AND pinned here longer than :func:`stall_seconds`; oldest first. The clock starts at the latest of
    ``ready_since`` (else creation), the newest placement event naming this machine and the newest worker-placement
    observation of the card not pinned here (:func:`_seen_unpinned_here_ms`)."""
    from hermes_cli import kanban_worker_node as wn
    now_ms = _now_ms(now)
    limit_ms = stall_seconds() * 1000
    out = []
    for row in wn.pinned_rows(replica, own, now_ms=now_ms, statuses=("ready",)):
        since = row["ready_since"] if row["ready_since"] is not None else int(row["created_at"] or 0) * 1000
        since = max(int(since), _pinned_here_since_ms(replica, row["id"], own) or 0,
                    _seen_unpinned_here_ms(replica, row["id"], own) or 0)
        if now_ms - int(since) <= limit_ms:
            continue
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (row["id"],)).fetchone():
            continue
        out.append({"id": row["id"], "title": row["title"], "tenant": row["tenant"], "since_ms": int(since),
                    "skip": latest(conn, row["id"])})
    out.sort(key=lambda c: (c["since_ms"], c["id"]))
    return out


def _covering_repair(conn, key: str, now_ms: int) -> Optional[str]:
    """The open incident for this exact detector subject; closed cards never cover a new firing."""
    row = conn.execute("SELECT id FROM tasks WHERE created_by = ? "
                       "AND idempotency_key >= ? AND idempotency_key < ? "
                       "AND status NOT IN ('done', 'archived') "
                       "ORDER BY created_at, id LIMIT 1", (DETECTOR, key, key + "\U0010ffff")).fetchone()
    return row["id"] if row is not None else None


def _minutes(since_ms: int, now_ms: int) -> int:
    return max(0, (now_ms - since_ms) // 60000)


def _tail(conn, own: str, howto: Optional[str] = None) -> str:
    """The repair runs on another machine (:func:`alarm_placement`), so name where ``own``'s machine-local
    evidence lives instead of saying "on this host"."""
    from hermes_cli import kanban_pickup_stall_repin as repin
    from hermes_cli import kanban_worker_node as wn
    try:
        replica = str(wn._replica_path(conn))
    except Exception:  # health: allow BLE001 -- a path in prose must never stop the alarm
        replica = "~/.hermes/kanban/sync-shadow/<board>.db"
    return (f"\n\nThis repair runs on another machine. The pickup skips quoted above are machine-local to {own} "
            f"(table `{TABLE}` in its live board file, not synced); read more there with `tailnet_exec` or ssh: "
            f"`kanban_show <card>` on {own} returns its `pickup_skip`, and {own}'s replica is `{replica}`. The "
            "Board row from any machine: `Hub(load_identity()).row_evidence(<board>, 'tasks', <card>)`. Check "
            "`kanban.sync.pickup`, the machine registry entry (labels, hold, pickup), profiles, capabilities and "
            "dispatcher capacity, fix the cause (or re-place the card on a machine that can run it), and verify the "
            "card is imported and claimed. " + (howto or repin.howto(own)) + " Do not claim a cause the recorded "
            "evidence does not show, and never lift or shorten a registry hold you did not set. This is agent-owned "
            "repair, not a human decision.")


# --- where the alarm runs ------------------------------------------------------------------------------------
# t_01a1ce5f6ddc72f6 (2026-11-12): every alarm was pinned ``machine:<own>``, the machine that takes no Board work
# (held, or no spawn slot), so it sat ready until the condition cleared by itself and nobody re-placed the cards.
OPERATOR_LANE = "hub"
FRESH_SECONDS = 300
_REQUIRES = ("terminal", "file", "kanban")
HUB_ACCESS = "capability:hub-write"


def _free_slots(entry: Mapping[str, Any]) -> Optional[int]:
    cap, active = entry.get("max_card_runs"), (entry.get("metrics") or {}).get("active_card_runs")
    if isinstance(cap, bool) or not isinstance(cap, int) or isinstance(active, bool) \
            or not isinstance(active, (int, float)):
        return None
    return cap - int(active)


def _takes_work(name: str, entry: Optional[Mapping[str, Any]], own: str, need: set[str]) -> bool:
    """Another active, unheld machine that copies cards and carries ``need`` (also the rule for where an alarm's
    run may re-place a stalled card: :mod:`hermes_cli.kanban_pickup_stall_repin`)."""
    from hermes_cli import kanban_worker_node as wn
    if name == own or not entry or entry.get("state", "active") != "active" or entry.get("hold") \
            or not wn.advertises_pickup(entry):
        return False
    return need <= wn.effective_labels(entry, name)


def _can_host(name: str, entry: Optional[Mapping[str, Any]], own: str, need: set[str]) -> bool:
    """:func:`_takes_work`, and reaches the hub (it is the operator lane, or attests ``capability:hub-write``):
    re-placing or handing off another gateway's card goes through it."""
    from hermes_cli import kanban_worker_node as wn
    return _takes_work(name, entry, own, need) and (name == OPERATOR_LANE
                                                    or HUB_ACCESS in wn.effective_labels(entry, name))


def _live(entry: Mapping[str, Any], now_ms: int) -> bool:
    """Not draining and seen within :data:`FRESH_SECONDS` (also the held-default taker test:
    :func:`hermes_cli.kanban_pickup_stall_held.others_take_default`)."""
    if (entry.get("metrics") or {}).get("drain_active"):
        return False
    seen = entry.get("last_seen")
    return not (isinstance(seen, (int, float)) and not isinstance(seen, bool) and now_ms - seen > FRESH_SECONDS * 1000)


def _ready_now(entry: Mapping[str, Any], now_ms: int) -> bool:
    """:func:`_live` and (when the registry says) a free card slot."""
    if not _live(entry, now_ms):
        return False
    free = _free_slots(entry)
    return free is None or free > 0


def _profile_label(assignee: Optional[str]) -> str:
    """:func:`hermes_cli.kanban_repin_notice.profile_label`, the Board's ``profile:<assignee or 'default'>``."""
    from hermes_cli import kanban_repin_notice
    return kanban_repin_notice.profile_label(assignee)


def alarm_placement(own: str, machines: Optional[Mapping[str, Mapping[str, Any]]], *, assignee: Optional[str],
                    requires: tuple[str, ...] = _REQUIRES, now_ms: Optional[int] = None,
                    ) -> tuple[Optional[list[str]], str]:
    """Queue on the capable worker pool, regardless of current capacity or registry availability.

    Negative eligibility is enforced independently from these positive labels by
    kanban_pickup_alarm_identity and the Board's authenticated detector provenance gate.
    A free hub is never a reason to pin an alarm there.
    """
    from hermes_cli.kanban_placement_coordination import requirement_label
    need = {requirement_label(r) for r in requires} | {_profile_label(assignee)}
    return sorted(need | {"pool:worker", HUB_ACCESS}), (
        f"queued for a capable pool worker other than authenticated stalled source {own}; "
        "capacity and registry availability do not change eligibility")


def _stranded(runs_on: Any, own: str, machines: Optional[Mapping[str, Mapping[str, Any]]], need: set[str]) -> bool:
    """An open alarm's pin cannot run it: it names ``own``, or (registry readable) a machine that cannot host."""
    from hermes_cli import kanban_worker_node as wn
    pins = [lab.split(":", 1)[1] for lab in wn._parse(runs_on) or [] if lab.startswith("machine:")]
    if own in pins:
        return True
    return machines is not None and any(not _can_host(p, machines.get(p), own, need) for p in pins)


def _replace_stranded(conn, task_id: str, own: str, machines, runs_on: list[str], why: str) -> None:
    """Re-pin an open, unclaimed alarm whose pin is stranded (legacy ``machine:<own>`` cards included)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli.kanban_db_comment_ids import add_comment
    from hermes_cli.kanban_placement_coordination import requirement_label
    with kb.write_txn(conn):
        row = conn.execute("SELECT status, claim_lock, runs_on, assignee FROM tasks WHERE id = ?",
                           (task_id,)).fetchone()
        if row is None or row["status"] in ("running", "done", "archived") or row["claim_lock"] is not None:
            return
        need = {requirement_label(r) for r in _REQUIRES} | {_profile_label(row["assignee"])}
        if not _stranded(row["runs_on"], own, machines, need) or wn._parse(row["runs_on"]) == runs_on:
            return
        wn.set_runs_on(conn, task_id, runs_on)
        add_comment(conn, task_id, DETECTOR, f"Re-placed from runs_on {row['runs_on']} to {runs_on}: {why}. "
                                             f"An alarm about {own} must not wait on {own}.")
    _log.info("pickup-stall re-placed card=%s runs_on=%s", task_id, runs_on)


def _create(conn, *, key: str, title: str, body: str, tenant: Optional[str], own: str, now_ms: int,
            priority: int = 0, howto: Optional[str] = None) -> str:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli.kanban_triage_sweep import _supervisor_profile
    reg = wn.registry()
    machines = None if reg is None else (reg.get("machines") or {})
    assignee = _supervisor_profile()
    runs_on, why = alarm_placement(own, machines, assignee=assignee, now_ms=now_ms)
    from hermes_cli.kanban_pickup_observation import append_observation
    from uuid import uuid4
    with kb.write_txn(conn):
        existing = _covering_repair(conn, key, now_ms)
        if existing:
            append_observation(conn, existing, DETECTOR, body)
        else:
            # Positive decimal suffix preserves the authenticated Board alarm identity contract.
            return kb.create_task(conn, title=title, body=body + f"\n\nPlaced with runs_on {runs_on}: {why}."
                                  + _tail(conn, own, howto), assignee=assignee, created_by=DETECTOR,
                                  workspace_kind="scratch", tenant=tenant, requires=list(_REQUIRES),
                                  idempotency_key=f"{key}{uuid4().int}", runs_on=runs_on, priority=priority)
    _replace_stranded(conn, existing, own, machines, runs_on, why)
    return existing


# Host-wide causes: one card per host, never one per pinned card. ``hold`` is an operator's deliberate state.
_HOST_GROUPS = {
    "evidence_missing": ("evidence-missing", "Pickup evidence missing on {own}",
                         "the detector has observed missing pickup evidence for an additional stall interval. "
                         "This is not a recorded pickup refusal: inspect pickup exceptions/timeouts and the "
                         "dispatcher logs before attributing a cause."),
    "budget_exhausted": (QUEUE_FULL, "Pickup queue full on {own}",
                         "the last pickup skip of each is budget_exhausted (no spawn slot on {own}). Decide "
                         "whether the queue drains on its own (running cards near done) or work must move to a "
                         "machine with slots (the kanban_move command below)."),
    "hold": ("hold", "Pickup held on {own}",
             "{own} is on hold in the machine registry, so it takes no Board work. An operator set that on "
             "purpose: do not lift it; re-place cards that cannot wait for the hold to end with the kanban_move "
             "command below."),
    "registry": ("registry", "Pickup blind on {own}: machine registry unavailable",
                 "{own} could not read the machine registry, so its pickup copies nothing. Restore registry access "
                 "(Board identity, network) and confirm the next pass imports the cards."),
}


def _registry_line(own: str) -> str:
    """``own``'s registry entry as the detector read it (hold, who and when, card slots): evidence for a repair
    that runs elsewhere."""
    from hermes_cli import kanban_worker_node as wn
    entry = ((wn.registry() or {}).get("machines") or {}).get(own)
    if not entry:
        return f"Registry entry for {own}: unreadable from {own}."
    keys = ("state", "hold", "hold_reason", "hold_by", "hold_at", "hold_owner", "pickup", "max_card_runs")
    fields = ", ".join(f"{k}={entry.get(k)!r}" for k in keys if k in entry)
    active = (entry.get("metrics") or {}).get("active_card_runs")
    # The task/comment creation time records when we observed these fields.
    return f"Registry entry for {own}: {fields}, active_card_runs={active!r}."


# --- expected holds ------------------------------------------------------------------------------------------
# t_01a1cedb2faf7341 (2026-11-12): hub-worker-core-rollout holds ONE worker at a time to drain it for a core update
# (owner ``rollout:worker-core-rollout@<host>``, reason ``...; release by HH:MMZ``, released within 90 min), and each
# rollout filed a "Pickup held on worker-N" repair (t_01a1cec7cd7fe7ce) whose answer is always "expected". Same rule
# as state's worker-claim-watch: a hold that states its release, or is the rollout's own, is expected until that
# release plus the stall threshold; past it, the hold outlived its word and alerts as before.
ROLLOUT_OWNER_PREFIX = "rollout:worker-core-rollout@"
ROLLOUT_MAX_SECONDS = 90 * 60  # the rollout's hard cap: its hold's implied release when the reason states none
_RELEASE_RE = re.compile(r"release(?:d)?\s+(?:by|at)\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*Z", re.IGNORECASE)


def _release_s(reason: Optional[str], held_at: float) -> Optional[float]:
    """First UTC ``release by HH:MM[:SS]Z`` instant at/after the hold start (None: no valid stated release)."""
    m = _RELEASE_RE.search(reason or "")
    if not m:
        return None
    h, mi, sec = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if h > 23 or mi > 59 or sec > 59:
        return None
    t = held_at - (held_at % 86400) + h * 3600 + mi * 60 + sec
    return t if t >= held_at else t + 86400


def expected_hold(entry: Optional[Mapping[str, Any]], *, now_ms: int, stall_s: int) -> Optional[float]:
    """The release instant (epoch s) of ``entry``'s hold while it is still expected, else None (alert as usual).

    Expected: a hold with a known start (``hold_at``, Board epoch ms) whose reason states ``release by HH:MMZ``,
    or whose owner is the core rollout (implied release: start + :data:`ROLLOUT_MAX_SECONDS`), until that release
    plus ``stall_s``. An unknown start, no release and no rollout owner, or a passed window fail toward alerting."""
    if not isinstance(entry, Mapping) or not entry.get("hold"):
        return None
    at = entry.get("hold_at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at <= 0:
        return None
    held_at = at / 1000.0 if at > 1e11 else float(at)
    reason = entry.get("hold_reason") if isinstance(entry.get("hold_reason"), str) else None
    release = _release_s(reason, held_at)
    if release is None and str(entry.get("hold_owner") or "").startswith(ROLLOUT_OWNER_PREFIX):
        release = held_at + ROLLOUT_MAX_SECONDS
    if release is None or now_ms > (release + stall_s) * 1000:
        return None
    return release


def _hold_expected(own: str, now_ms: int) -> bool:
    """``own``'s registry hold is inside its stated window (:func:`expected_hold`): file no hold repair now."""
    from hermes_cli import kanban_worker_node as wn
    entry = ((wn.registry() or {}).get("machines") or {}).get(own)
    release = expected_hold(entry, now_ms=now_ms, stall_s=stall_seconds())
    if release is None:
        return False
    _log.debug("pickup-stall: %s hold by %s is expected until %s + %ss; no repair", own,
               (entry or {}).get("hold_owner") or (entry or {}).get("hold_by"), _stamp(int(release * 1000)),
               stall_seconds())
    return True


def _hold_lifted(own: str) -> bool:
    """``own``'s live registry entry is readable and holds nothing: a ``hold`` skip predates the pass, so it files no
    hold repair (the card's next pickup skip classifies it). An unreadable registry fails toward alerting.

    t_01a1d5b42df173d3 (2026-11-13): worker-6 filed "Pickup held on worker-6" at 15:45:09Z from hold skips last
    recorded at 15:43:45Z while the entry quoted in its own body read hold=False; :func:`expected_hold` returns
    None for a lifted hold, which read as "alert as usual"."""
    from hermes_cli import kanban_pickup_stall_resolve as resolve
    lifted = resolve._hold_lifted(own)
    if lifted:
        _log.debug("pickup-stall: %s hold has lifted (%s); no hold repair", own, lifted)
    return lifted is not None


_QUOTA_UNTIL = re.compile(r" until (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)Z")


def _quota_hold_expected(skip: Mapping[str, Any], now_ms: int) -> bool:
    """A ``quota_hold`` skip (the card's own quota wall: local dispatch would hold a copy) whose stated deadline plus
    the stall threshold is still ahead: the wait is expected, file nothing. Past it, or with no readable deadline,
    the card alerts on its own like any card-specific reason, never as the machine-wide registry ``hold``."""
    found = _QUOTA_UNTIL.search(str(skip.get("detail") or "")) if skip.get("reason") == "quota_hold" else None
    if found is None:
        return False
    from datetime import UTC, datetime
    until = datetime.strptime(found.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).timestamp()
    return now_ms < (until + stall_seconds()) * 1000


def _group(card: Mapping[str, Any]) -> Optional[str]:
    skip = card["skip"] or {}
    if skip.get("reason") == "other" and skip.get("detail") == "machine registry unavailable":
        return "registry"
    return skip.get("reason") if skip.get("reason") in ("budget_exhausted", "hold", "evidence_missing") else None


def _refresh_targets(conn, task_id: str, members: list[dict[str, Any]], now_ms: int) -> None:
    """Append newly affected targets only; unchanged ticks must not churn the thread."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_comment_ids import add_comment
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        if task is None or task.status in ("done", "archived"):
            return
        seen = (task.body or "") + "\n" + "\n".join(
            c.body for c in kb.list_comments(conn, task_id) if c.author == DETECTOR)
        added = [c for c in members if f"- {c['id']} (" not in seen]
        if added:
            lines = "\n".join(f"- {c['id']} ({c['title']}), ready {_minutes(c['since_ms'], now_ms)} min, "
                              f"last skip {describe(c.get('skip'))}" for c in added)
            add_comment(conn, task_id, DETECTOR, "Additional targets for this pickup cause:\n\n" + lines)
            _log.info("pickup-stall refreshed card=%s added_targets=%d", task_id, len(added))


def _open_coverage(conn, own: str) -> dict[str, str]:
    """``{target card id: slug}`` for every card an OPEN host-wide repair about ``own`` (:data:`_HOST_GROUPS`) lists
    in its body or the detector's target comments: a card whose host-wide cause changed (evidence missing, then
    queue full) is not handed a second open host-wide repair. A card-specific refusal still gets its own card.

    t_01a1cfdfb6f3f7ea (2026-11-12): worker-2 filed "Pickup evidence missing" for t_01a1cfa91548dad7 at 12:50:44Z;
    pickup recorded budget_exhausted 0.3 s later, and the next tick filed "Pickup queue full" for the same card."""
    from hermes_cli import kanban_pickup_stall_resolve as resolve
    out: dict[str, str] = {}
    for _, slug, targets in resolve.open_host_repairs(conn, own):
        for tid in targets:
            out.setdefault(tid, slug)
    return out


def _start_gate_expected(skip: Mapping[str, Any]) -> bool:
    """Pickup skipped the card because a plugin start gate holds this machine's card starts (``kanban_start_gate``;
    pickup records ``other`` with detail ``start_gate:<word> (...)``). That is a deliberate fleet-wide wait, e.g.
    the token throttle while spend is over its cap: a repair card per pinned card would only add work to a fleet
    that is being told to start none. The gate is asked again every pass, so the skip ends with it."""
    return skip.get("reason") == "other" and str(skip.get("detail") or "").startswith("start_gate:")


def file_repairs(conn, own: str, cards: list[dict[str, Any]], *, now: Optional[float] = None) -> list[str]:
    """One repair per stalled card with a card-specific last reason; one per host per host-wide cause
    (:data:`_HOST_GROUPS`), naming the oldest card. A card an open repair about this host already lists under
    another key gets no second one (:func:`_open_coverage`). Returns the covering repair ids."""
    now_ms = _now_ms(now)
    ids: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    covered = _open_coverage(conn, own)
    for card in cards:
        # The sweep precedes pickup/note_capped. A first post-drain tick may
        # have old ready rows but no observation yet: missing evidence is not
        # a target-specific cause. Persist the first observation so a crashing
        # pickup cannot suppress alerts forever (including across restarts).
        if not card["skip"]:
            record(conn, {card["id"]: ("evidence_missing", "detector observation; no pickup skip recorded")}, now=now)
            continue
        if (card["skip"]["reason"] == "evidence_missing"
                and now_ms - card["skip"]["first_at"] < stall_seconds() * 1000):
            continue
        if _quota_hold_expected(card["skip"], now_ms) or _start_gate_expected(card["skip"]):
            continue
        group = _group(card)
        if group is not None:
            if covered.get(card["id"], _HOST_GROUPS[group][0]) != _HOST_GROUPS[group][0]:
                continue  # an open host-wide repair about this machine already lists it
            groups.setdefault(group, []).append(card)
            continue
        reason = (card["skip"] or {}).get("reason") or "none recorded"
        waited = _minutes(card["since_ms"], now_ms)
        if reason == "guard_held":
            ids.append(_create(
                conn, key=f"{KEY_PREFIX}{own}:{card['id']}:", tenant=card["tenant"], own=own, now_ms=now_ms,
                title=(f"Pickup stalled: {card['id']} pinned to machine:{own}, ready {waited} min, held by a "
                       f"respawn guard ({card['skip'].get('detail') or 'guard_held'})"),
                body=(f"Card {card['id']} ({card['title']}) has been ready on the Board, pinned to machine:{own}, "
                      f"for {waited} min (threshold {stall_seconds()} s). {own} is not on a registry hold: the "
                      f"card's Board history shows a dispatcher respawn guard holding it, and that hold lifts when "
                      f"the guard does. Check whether the guard named below is still right (an open PR, a quota "
                      f"reset clock, a shared blocker) rather than the machine.\n\nLast pickup skip recorded on "
                      f"{own}: {describe(card['skip'])}.")))
            continue
        ids.append(_create(
            conn, key=f"{KEY_PREFIX}{own}:{card['id']}:", tenant=card["tenant"], own=own, now_ms=now_ms,
            title=f"Pickup stalled: {card['id']} pinned to machine:{own}, ready {waited} min, not imported ({reason})",
            body=(f"Card {card['id']} ({card['title']}) has been ready on the Board, pinned to machine:{own}, "
                  f"for {waited} min (threshold {stall_seconds()} s) and {own} has not imported or claimed "
                  f"it.\n\nLast pickup skip recorded on {own}: {describe(card['skip'])}.")))
    for group, members in groups.items():
        slug, title, why = _HOST_GROUPS[group]
        if group == "hold" and (_hold_expected(own, now_ms) or _hold_lifted(own)):
            continue
        oldest = members[0]
        # The watcher deduplicates bodies: age alone is not a new observation.
        listed = "\n".join(f"- {c['id']} ({c['title']}), ready since {_stamp(c['since_ms'])}, "
                           f"last skip {describe(c['skip'])}" for c in members[:20])
        # Below ordinary work: a host-wide repair must not take the slot the cards it reports on are waiting for.
        ids.append(_create(
            conn, key=f"{KEY_PREFIX}{own}:{slug}:", tenant=oldest["tenant"], own=own, now_ms=now_ms, priority=-1,
            title=(title.format(own=own) + f": oldest {oldest['id']} waiting "
                   f"{_minutes(oldest['since_ms'], now_ms)} min ({len(members)} pinned card(s))"),
            body=(f"{len(members)} card(s) pinned to machine:{own} have waited past {stall_seconds()} s, and "
                  f"{why.format(own=own)}\n\nOldest: {oldest['id']} ({oldest['title']}), last skip "
                  f"{describe(oldest['skip'])}.\n\n{_registry_line(own)}\n\nPickup skips recorded on {own}:\n\n"
                  f"{listed}")))
        _refresh_targets(conn, ids[-1], members, now_ms)
    return ids



def sweep(conn, *, now: Optional[float] = None) -> list[str]:
    """The detector pass (see the module doc). Returns the repair card ids now open for stalled cards."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn
    if sync.board_mode(conn) != "primary":
        return []
    from hermes_cli import kanban_pickup_stall_home as home_stalls
    stranded = home_stalls.observe(conn, now_ms=_now_ms(now))
    if wn.pickup_mode() is None:
        return stranded
    from hermes_cli.kanban_gateway_drain import dispatch_paused
    if dispatch_paused():
        return stranded  # local pickup evidence stops; cross-home observation is independent
    _prune(conn, _now_ms(now))
    own = wn._own(conn)
    from hermes_cli import kanban_pickup_stall_held as held  # own default-placement cards on a held machine
    stranded += held.sweep(conn, own, now_ms=_now_ms(now))
    replica = wn.open_replica(conn)
    if replica is None:
        return stranded
    try:
        resolve_sweep(conn, replica, own, now_ms=_now_ms(now))
        cards = stalled(conn, replica, own, now=now)
    finally:
        replica.close()
    return stranded + (file_repairs(conn, own, cards, now=now) if cards else [])


def resolve_sweep(conn, replica, own: str, *, now_ms: int) -> list[str]:
    """:func:`hermes_cli.kanban_pickup_stall_resolve.sweep`: close host-wide repairs with nothing left to repair,
    every pass and before filing, so a closed hold repair never hides the next reason's alarm."""
    from hermes_cli import kanban_pickup_stall_resolve as resolve
    try:
        return resolve.sweep(conn, replica, own, now_ms=now_ms)
    except Exception:  # health: allow BLE001 -- closing a moot alarm must never stop the detector
        _log.warning("kanban dispatcher: pickup-stall resolve failed", exc_info=True)
        return []


def safe_sweep(conn) -> list[str]:
    try:
        return sweep(conn)
    except Exception:  # health: allow BLE001 -- the detector must never stop dispatch
        _log.warning("kanban dispatcher: pickup-stall sweep failed", exc_info=True)
        return []
