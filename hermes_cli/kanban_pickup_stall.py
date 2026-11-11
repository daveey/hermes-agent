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
  ``ready_since`` is older than ``kanban.sync.pickup_stall_seconds`` (default 600) gets ONE agent repair card on this
  host (an open one, or one closed less than :data:`REPLACE_AFTER_SECONDS` ago, is reused). Host-wide causes share
  ONE card per host naming the oldest card: ``budget_exhausted`` (the queue is simply full), ``hold`` and an
  unreadable machine registry. ``budget_exhausted`` is recorded only for a card a slot would have started, so a
  refusal is never hidden behind a full queue. No Inbox post, no human ask.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Mapping, Optional

_log = logging.getLogger(__name__)

TABLE = "kanban_pickup_skips"
REASONS = ("budget_exhausted", "profile_ineligible", "capability_missing", "parents_incomplete", "hold", "other")
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


_TAIL = ("\n\nOn this host: `kanban_show <card>` returns its `pickup_skip`; the replica is "
         "`~/.hermes/kanban/sync-shadow/<board>.db`; the Board row via "
         "`Hub(load_identity()).row_evidence(<board>, 'tasks', <card>)`. Check `kanban.sync.pickup`, the machine "
         "registry entry (labels, hold, pickup), profiles, capabilities and dispatcher capacity, fix the cause "
         "(or re-place the card on a machine that can run it), and verify the card is imported and claimed. Do not "
         "claim a cause the recorded evidence does not show, and never lift a registry hold you did not set. This "
         "is agent-owned repair, not a human decision.")


def _create(conn, *, key: str, title: str, body: str, tenant: Optional[str], own: str, now_ms: int,
            priority: int = 0) -> str:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_triage_sweep import _supervisor_profile
    existing = _covering_repair(conn, key, now_ms)
    if existing:
        return existing
    # A repair closed long ago is replaced, so the key carries the creation time (create_task returns any
    # non-archived task with an equal key, done ones included).
    return kb.create_task(conn, title=title, body=body + _TAIL, assignee=_supervisor_profile(), created_by=DETECTOR,
                          workspace_kind="scratch", tenant=tenant, requires=["terminal", "file", "kanban"],
                          idempotency_key=f"{key}{_now_ms()}", runs_on=[f"machine:{own}"], priority=priority)


# Host-wide causes: one card per host, never one per pinned card. ``hold`` is an operator's deliberate state.
_HOST_GROUPS = {
    "budget_exhausted": (QUEUE_FULL, "Pickup queue full on {own}",
                         "the last pickup skip of each is budget_exhausted (no spawn slot on this host). Decide "
                         "whether the queue drains on its own (running cards near done) or work must move to a "
                         "machine with slots."),
    "hold": ("hold", "Pickup held on {own}",
             "this machine is on hold in the machine registry, so it takes no Board work. An operator set that on "
             "purpose: do not lift it; re-place cards that cannot wait for the hold to end."),
    "registry": ("registry", "Pickup blind on {own}: machine registry unavailable",
                 "the machine registry could not be read, so pickup copies nothing. Restore registry access "
                 "(Board identity, network) and confirm the next pass imports the cards."),
}


def _group(card: Mapping[str, Any]) -> Optional[str]:
    skip = card["skip"] or {}
    if skip.get("reason") == "other" and skip.get("detail") == "machine registry unavailable":
        return "registry"
    return skip.get("reason") if skip.get("reason") in ("budget_exhausted", "hold") else None


def file_repairs(conn, own: str, cards: list[dict[str, Any]], *, now: Optional[float] = None) -> list[str]:
    """One repair per stalled card with a card-specific last reason; one per host per host-wide cause
    (:data:`_HOST_GROUPS`), naming the oldest card. Returns the covering repair ids."""
    now_ms = _now_ms(now)
    ids: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
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
                  f"for {waited} min (threshold {stall_seconds()} s) and this host has not imported or claimed "
                  f"it.\n\nLast recorded pickup skip: {describe(card['skip'])}.")))
    for group, members in groups.items():
        slug, title, why = _HOST_GROUPS[group]
        oldest = members[0]
        listed = "\n".join(f"- {c['id']} ({c['title']}), ready {_minutes(c['since_ms'], now_ms)} min"
                           for c in members[:20])
        # Below ordinary work: a host-wide repair must not take the slot the cards it reports on are waiting for.
        ids.append(_create(
            conn, key=f"{KEY_PREFIX}{own}:{slug}:", tenant=oldest["tenant"], own=own, now_ms=now_ms, priority=-1,
            title=(title.format(own=own) + f": oldest {oldest['id']} waiting "
                   f"{_minutes(oldest['since_ms'], now_ms)} min ({len(members)} pinned card(s))"),
            body=(f"{len(members)} card(s) pinned to machine:{own} have waited past {stall_seconds()} s, and {why}"
                  f"\n\nOldest: {oldest['id']} ({oldest['title']}), last skip {describe(oldest['skip'])}."
                  f"\n\n{listed}")))
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
