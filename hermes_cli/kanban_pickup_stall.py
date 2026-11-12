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
  ``ready_since`` is older than ``kanban.sync.pickup_stall_seconds`` (default 600) gets ONE agent repair card filed
  by this host (an open one, or one closed less than :data:`REPLACE_AFTER_SECONDS` ago, is reused). The repair is
  never pinned here (:func:`alarm_placement`): this host is the one taking no Board work, so the repair is pinned
  to another active, unheld machine that can run it now, or the hub operator lane, and an open unclaimed repair
  whose pin is stranded is re-pinned. Its body carries this host's machine-local evidence (pickup skips, registry
  entry, replica path). Host-wide causes share
  ONE card per host naming the oldest card: ``budget_exhausted`` (the queue is simply full), ``hold`` and an
  unreadable machine registry. A ``hold`` that states ``release by HH:MMZ`` (or is the core rollout's own,
  :func:`expected_hold`) files nothing until that release plus the stall threshold has passed. Missing evidence gets
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
           "evidence_missing")
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


def stalled(conn, replica, own: str, *, now: Optional[float] = None) -> list[dict[str, Any]]:
    """Cards ready in ``replica``, pinned ``machine:<own>``, created elsewhere, unleased, not in the live file,
    ready longer than :func:`stall_seconds`; oldest first."""
    from hermes_cli import kanban_worker_node as wn
    now_ms = _now_ms(now)
    limit_ms = stall_seconds() * 1000
    out = []
    for row in wn.pinned_rows(replica, own, now_ms=now_ms, statuses=("ready",)):
        since = row["ready_since"] if row["ready_since"] is not None else int(row["created_at"] or 0) * 1000
        if now_ms - int(since) <= limit_ms:
            continue
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (row["id"],)).fetchone():
            continue
        out.append({"id": row["id"], "title": row["title"], "tenant": row["tenant"], "since_ms": int(since),
                    "skip": latest(conn, row["id"])})
    out.sort(key=lambda c: (c["since_ms"], c["id"]))
    return out


def _covering_repair(conn, key: str, now_ms: int) -> Optional[str]:
    """The repair already covering ``key``: an open one, or one closed less than :data:`REPLACE_AFTER_SECONDS` ago
    (so an agent that finished one is not handed the same card on the next tick)."""
    row = conn.execute("SELECT id, status, COALESCE(completed_at, created_at) AS closed FROM tasks "
                       "WHERE idempotency_key >= ? AND idempotency_key < ? "
                       "ORDER BY created_at DESC, id DESC LIMIT 1", (key, key + "\U0010ffff")).fetchone()
    if row is None:
        return None
    if row["status"] not in ("done", "archived"):
        return row["id"]
    return row["id"] if now_ms - int(row["closed"] or 0) * 1000 < REPLACE_AFTER_SECONDS * 1000 else None


def _minutes(since_ms: int, now_ms: int) -> int:
    return max(0, (now_ms - since_ms) // 60000)


def _tail(conn, own: str) -> str:
    """The repair runs on another machine (:func:`alarm_placement`), so name where ``own``'s machine-local
    evidence lives instead of saying "on this host"."""
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
            "card is imported and claimed. Do not claim a cause the recorded evidence does not show, and never lift "
            "or shorten a registry hold you did not set. This is agent-owned repair, not a human decision.")


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


def _can_host(name: str, entry: Optional[Mapping[str, Any]], own: str, need: set[str]) -> bool:
    """Another active, unheld machine that copies cards, carries ``need`` and reaches the hub (it is the operator
    lane, or attests ``capability:hub-write``): re-placing or handing off another gateway's card goes through it."""
    from hermes_cli import kanban_worker_node as wn
    if name == own or not entry or entry.get("state", "active") != "active" or entry.get("hold") \
            or not wn.advertises_pickup(entry):
        return False
    labels = wn.effective_labels(entry, name)
    return need <= labels and (name == OPERATOR_LANE or HUB_ACCESS in labels)


def _ready_now(entry: Mapping[str, Any], now_ms: int) -> bool:
    """Not draining, seen recently and (when the registry says) a free card slot."""
    if (entry.get("metrics") or {}).get("drain_active"):
        return False
    seen = entry.get("last_seen")
    if isinstance(seen, (int, float)) and not isinstance(seen, bool) and now_ms - seen > FRESH_SECONDS * 1000:
        return False
    free = _free_slots(entry)
    return free is None or free > 0


def alarm_placement(own: str, machines: Optional[Mapping[str, Mapping[str, Any]]], *, assignee: str,
                    requires: tuple[str, ...] = _REQUIRES, now_ms: Optional[int] = None,
                    ) -> tuple[Optional[list[str]], str]:
    """``(runs_on, why)`` for an alarm about machine ``own``; never ``machine:<own>``. ``machines`` is the registry
    (None: unreadable). In order: the eligible-now host (:func:`_can_host` and :func:`_ready_now`) with the most
    free slots; the hub operator lane; any other host; any other machine that copies cards, even held (the hub
    first: it runs the alarm when that hold ends, not ``own``'s). With no other such machine enrolled, None
    (default placement): the registry offers nowhere else."""
    from hermes_cli import kanban_worker_node as wn
    now_ms = _now_ms() if now_ms is None else now_ms
    need = {f"capability:{r}" for r in requires} | {f"profile:{assignee}"}
    if machines is None:
        if own != OPERATOR_LANE:
            return [f"machine:{OPERATOR_LANE}"], f"the machine registry is unreadable from {own}: hub operator lane"
        # The hub carries no capability:hub-write, so this label set never lands back on it.
        return sorted(need | {HUB_ACCESS}), "the registry is unreadable from the hub: any worker with hub access"
    hosts = {n: e for n, e in machines.items() if _can_host(n, e, own, need)}
    now = sorted(n for n, e in hosts.items() if _ready_now(e, now_ms))
    if now:
        best = min(now, key=lambda n: (-(_free_slots(hosts[n]) or 0), n))
        free = _free_slots(hosts[best])
        return [f"machine:{best}"], (f"{best} is active, unheld, not draining"
                                     + (f" and has {free} free card slot(s)" if free is not None else ""))
    if OPERATOR_LANE in hosts:
        return [f"machine:{OPERATOR_LANE}"], "no other machine is eligible now: hub operator lane"
    if hosts:
        best = min(hosts, key=lambda n: (-(_free_slots(hosts[n]) or 0), n))
        return [f"machine:{best}"], f"no other machine is eligible now; {best} is unheld and will free up first"
    held = sorted((n != OPERATOR_LANE, n) for n, e in machines.items() if n != own and e
                  and e.get("state", "active") == "active" and wn.advertises_pickup(e))
    if held:
        name = held[0][1]
        return [f"machine:{name}"], f"every other machine is held or lacks access; {name} takes it when free"
    return None, f"no other machine copies cards: default placement (the registry offers nowhere but {own})"


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
    with kb.write_txn(conn):
        row = conn.execute("SELECT status, claim_lock, runs_on, assignee FROM tasks WHERE id = ?",
                           (task_id,)).fetchone()
        if row is None or row["status"] in ("running", "done", "archived") or row["claim_lock"] is not None:
            return
        need = {f"capability:{r}" for r in _REQUIRES} | {f"profile:{row['assignee']}"}
        if not _stranded(row["runs_on"], own, machines, need) or wn._parse(row["runs_on"]) == runs_on:
            return
        wn.set_runs_on(conn, task_id, runs_on)
        add_comment(conn, task_id, DETECTOR, f"Re-placed from runs_on {row['runs_on']} to {runs_on}: {why}. "
                                             f"An alarm about {own} must not wait on {own}.")
    _log.info("pickup-stall re-placed card=%s runs_on=%s", task_id, runs_on)


def _create(conn, *, key: str, title: str, body: str, tenant: Optional[str], own: str, now_ms: int,
            priority: int = 0) -> str:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli.kanban_triage_sweep import _supervisor_profile
    reg = wn.registry()
    machines = None if reg is None else (reg.get("machines") or {})
    assignee = _supervisor_profile()
    runs_on, why = alarm_placement(own, machines, assignee=assignee, now_ms=now_ms)
    existing = _covering_repair(conn, key, now_ms)
    if existing:
        _replace_stranded(conn, existing, own, machines, runs_on, why)
        return existing
    # A repair closed long ago is replaced, so the key carries the creation time (create_task returns any
    # non-archived task with an equal key, done ones included).
    return kb.create_task(conn, title=title, body=body + f"\n\nPlaced with runs_on {runs_on}: {why}."
                          + _tail(conn, own), assignee=assignee, created_by=DETECTOR, workspace_kind="scratch",
                          tenant=tenant, requires=list(_REQUIRES), idempotency_key=f"{key}{_now_ms()}",
                          runs_on=runs_on, priority=priority)


# Host-wide causes: one card per host, never one per pinned card. ``hold`` is an operator's deliberate state.
_HOST_GROUPS = {
    "evidence_missing": ("evidence-missing", "Pickup evidence missing on {own}",
                         "the detector has observed missing pickup evidence for an additional stall interval. "
                         "This is not a recorded pickup refusal: inspect pickup exceptions/timeouts and the "
                         "dispatcher logs before attributing a cause."),
    "budget_exhausted": (QUEUE_FULL, "Pickup queue full on {own}",
                         "the last pickup skip of each is budget_exhausted (no spawn slot on {own}). Decide "
                         "whether the queue drains on its own (running cards near done) or work must move to a "
                         "machine with slots."),
    "hold": ("hold", "Pickup held on {own}",
             "{own} is on hold in the machine registry, so it takes no Board work. An operator set that on "
             "purpose: do not lift it; re-place cards that cannot wait for the hold to end."),
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
        return f"Registry entry for {own}: unreadable from {own} at {_stamp(_now_ms())}."
    keys = ("state", "hold", "hold_reason", "hold_by", "hold_at", "hold_owner", "pickup", "max_card_runs")
    fields = ", ".join(f"{k}={entry.get(k)!r}" for k in keys if k in entry)
    active = (entry.get("metrics") or {}).get("active_card_runs")
    return f"Registry entry for {own} at {_stamp(_now_ms())}: {fields}, active_card_runs={active!r}."


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
    if not isinstance(entry, Mapping) or entry.get("hold") is not True:
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


def file_repairs(conn, own: str, cards: list[dict[str, Any]], *, now: Optional[float] = None) -> list[str]:
    """One repair per stalled card with a card-specific last reason; one per host per host-wide cause
    (:data:`_HOST_GROUPS`), naming the oldest card. Returns the covering repair ids."""
    now_ms = _now_ms(now)
    ids: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {}
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
        group = _group(card)
        if group is not None:
            groups.setdefault(group, []).append(card)
            continue
        reason = (card["skip"] or {}).get("reason") or "none recorded"
        waited = _minutes(card["since_ms"], now_ms)
        ids.append(_create(
            conn, key=f"{KEY_PREFIX}{own}:{card['id']}:", tenant=card["tenant"], own=own, now_ms=now_ms,
            title=f"Pickup stalled: {card['id']} pinned to machine:{own}, ready {waited} min, not imported ({reason})",
            body=(f"Card {card['id']} ({card['title']}) has been ready on the Board, pinned to machine:{own}, "
                  f"for {waited} min (threshold {stall_seconds()} s) and {own} has not imported or claimed "
                  f"it.\n\nLast pickup skip recorded on {own}: {describe(card['skip'])}.")))
    for group, members in groups.items():
        slug, title, why = _HOST_GROUPS[group]
        if group == "hold" and _hold_expected(own, now_ms):
            continue
        oldest = members[0]
        listed = "\n".join(f"- {c['id']} ({c['title']}), ready {_minutes(c['since_ms'], now_ms)} min, "
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
    if sync.board_mode(conn) != "primary" or wn.pickup_mode() is None:
        return []
    from hermes_cli.kanban_gateway_drain import dispatch_paused
    if dispatch_paused():
        return []  # a draining gateway runs no pickup, so it writes no fresh evidence to judge a stall by
    _prune(conn, _now_ms(now))
    replica = wn.open_replica(conn)
    if replica is None:
        return []
    try:
        own = wn._own(conn)
        cards = stalled(conn, replica, own, now=now)
    finally:
        replica.close()
    return file_repairs(conn, own, cards, now=now) if cards else []


def safe_sweep(conn) -> list[str]:
    try:
        return sweep(conn)
    except Exception:  # health: allow BLE001 -- the detector must never stop dispatch
        _log.warning("kanban dispatcher: pickup-stall sweep failed", exc_info=True)
        return []
