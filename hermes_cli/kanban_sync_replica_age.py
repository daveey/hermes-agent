"""Is each synced board's replica reaching the feed head? One card per incident when it stops. [daveey]

Incidents (2026-11-12, t_01a1d3df5e5d0757): on hermes-hub the default sync loop slowed from a round a minute to one
every 15-28 min and the replica's ``pulled_at`` stayed at 02:06:53 UTC past 02:39, so hub cards did not promote; on
David-MBP the replica sat ~75k changes behind for hours after a sleep; at 23:53:44 PT the same MacBook's loop refused
a newer core's schema (SchemaAhead) and ran no more rounds. Each was found by someone chasing a symptom: no component
watched the replica itself, and since #1931 the consumers that used to misfire on a stale replica wait silently.

:func:`check` runs on the dispatcher tick from ``kanban_sync_outbox_age.check``, outside the sync loop, so it sees a
dead or stuck loop too. Per board in mode ``primary`` or ``shadow`` it keeps ``(stamp, monotonic time first seen)``
for the replica's ``pulled_at`` (``_pulled_replica`` stamps it only when a pull reached the feed head). A missing or
unreadable replica counts as a stamp that has not advanced. When the stamp has not advanced for
:data:`REPLICA_STALE_S` of monotonic time an incident opens and files ONE card through the outbox alarm's report
path (comment at most hourly on an open card, a new card once it is done). Monotonic time stands still while a Mac
sleeps, so a laptop's sleep never counts. The incident ends (one INFO line) when the stamp advances.

One check covers every shape: rounds ending behind, rounds failing, no rounds at all (a dead loop or a give-up),
and a missing replica. The card names the host, board, stamp (PT), awake minutes, the last round's outcome, the
loop's recorded stop, the pending outbox, the running core and the cards frozen behind a remote parent.
"""
from __future__ import annotations

import logging
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger(__name__)

# Twice kanban_replica_freshness.FRESH_SECONDS (600): the missing-parent gate's own freshness bound.
REPLICA_STALE_S = 1200.0
# No completed round for this long reads as "no sync round since ...".
NO_ROUND_S = 300.0
TELEMETRY_EVERY_S = 3600.0
KEY = "kanban-sync-replica-stale"
AUTHOR = "kanban-sync-replica"
_FROZEN_CAP = 25
_PT = "America/Los_Angeles"


@dataclass
class _Seen:
    stamp: Optional[float]  # the replica's last pulled_at (None: no readable replica yet)
    since: float  # monotonic time the stamp was first seen (or the first observation)
    open: bool = False  # an incident is open (its card filed)


@dataclass
class _Round:
    mono: float
    wall: float
    error: Optional[str]
    behind: bool


_LOCK = threading.Lock()
_SEEN: dict[str, _Seen] = {}
_ROUNDS: dict[str, _Round] = {}
_TELEMETRY_AT: dict[str, float] = {}


def note_round(results: Iterable[Any], *, mono: Callable[[], float] = time.monotonic,
               wall: Callable[[], float] = time.time) -> None:
    """The sync loop completed a round (``kanban_sync_outbox_age.after_round``): remember each board's outcome."""
    with _LOCK:
        for r in results:
            _ROUNDS[r.board] = _Round(mono(), wall(), r.error, bool(r.behind))


def _pt(at: Optional[float]) -> str:
    if at is None:
        return "?"
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(at, ZoneInfo(_PT)).strftime("%Y-%m-%d %H:%M:%S %Z")


def _mode(path: Path) -> Optional[str]:
    """The live board's sync mode for this process (``off`` when detached, unselected or sync disabled)."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_sync as sync
    if not Path(path).exists():
        return None  # never create a board file just to judge it
    with kbc.connect_closing(path) as conn:
        return sync.board_mode(conn)


def _stamp(slug: str) -> Optional[float]:
    """The replica's ``pulled_at`` (None: no replica, unreadable, or never stamped at the head)."""
    from hermes_cli import kanban_sync_client as ksc
    path = ksc.shadow_path(slug)
    if not path.exists():
        return None
    from hermes_cli import kanban_lockguard
    try:
        conn = kanban_lockguard.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute("SELECT pulled_at FROM kanban_sync_state LIMIT 1").fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None
    return float(row[0]) if row and type(row[0]) in (int, float) else None


def _core_sha() -> Optional[str]:
    from hermes_cli.boot_bootstrap import read_git_head
    from hermes_cli.kanban_schema_owner import running_core
    head = read_git_head(running_core())
    return head[:12] if head else None


def check(boards: list[tuple[str, Path]], *, now: float, wall: Callable[[], float] = time.time,
          file_fn: Optional[Callable[[str, str, str], Optional[str]]] = None,
          board_status: Optional[Callable[[str, list[str]], dict[str, str]]] = None) -> list[str]:
    """One observation of every board (``now``: monotonic). Returns the card ids it filed or commented. Never
    raises."""
    out: list[str] = []
    for slug, path in boards:
        try:
            tid = _check_board(slug, path, now, wall, file_fn or report, board_status or _board_status)
            if tid:
                out.append(tid)
        except Exception:  # health: allow BLE001 -- an alert must never break the dispatcher tick
            log.warning("kanban sync: replica freshness check failed for %s", slug, exc_info=True)
    return out


def _check_board(slug: str, path: Path, now: float, wall: Callable[[], float], file_fn, board_status) -> Optional[str]:
    if _mode(path) not in ("primary", "shadow"):
        with _LOCK:
            _SEEN.pop(slug, None)  # detached or unsynced: nothing to judge, and a later attach starts afresh
        return None
    stamp = _stamp(slug)
    with _LOCK:
        seen = _SEEN.get(slug)
        # Any change counts: ``pulled_at`` is wall time, and a wall clock stepped back (NTP, a manual fix) writes
        # stamps below the remembered one on healthy rounds.
        if seen is None or (stamp is not None and (seen.stamp is None or stamp != seen.stamp)):
            if seen is not None and seen.open:
                log.info("kanban sync: %s replica reached the feed head again at %s after %.0f awake min; "
                         "incident over", slug, _pt(stamp), (now - seen.since) / 60)
            seen = _SEEN[slug] = _Seen(stamp, now)
        elapsed = now - seen.since
        due = elapsed >= REPLICA_STALE_S and not seen.open
        if due:
            seen.open = True
        rnd = _ROUNDS.get(slug)
    _telemetry(slug, seen, elapsed, now)
    if not due:
        return None
    detail = _detail(slug, path, seen, elapsed, rnd, now, wall())
    return file_fn(slug, detail, _body(slug, path, detail, board_status))


def _telemetry(slug: str, seen: _Seen, elapsed: float, now: float) -> None:
    """One INFO line per board per hour: the live proof that the check runs and what it sees."""
    last = _TELEMETRY_AT.get(slug)
    if last is not None and now - last < TELEMETRY_EVERY_S:
        return
    _TELEMETRY_AT[slug] = now
    log.info("kanban sync: replica freshness %s: stamp %s, not advanced for %.0f awake s (alarm at %.0f s), "
             "incident %s", slug, _pt(seen.stamp) if seen.stamp else "none", elapsed, REPLICA_STALE_S,
             "open" if seen.open else "none")


def _outcome(rnd: Optional[_Round], now: float, first_seen_wall: float) -> str:
    if rnd is None:
        return f"no sync round since {_pt(first_seen_wall)} (none completed in this process)"
    what = (f"failed: {rnd.error}" if rnd.error else
            "ended behind the feed head (page budget)" if rnd.behind else "reached the feed head")
    if now - rnd.mono >= NO_ROUND_S:
        return f"no sync round since {_pt(rnd.wall)} (that round {what})"
    return f"last round {what}"


def _pending(path: Path) -> Optional[int]:
    from hermes_cli import kanban_db_connect as kbc
    try:
        with kbc.connect_closing(path) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state = 'pending'").fetchone()[0])
    except (sqlite3.Error, OSError):
        return None


def _gateway(path: Path) -> str:
    """The board's own sync identity (``kanban_sync_state.gateway_id``), else the configured gateway."""
    from hermes_cli import kanban_db_connect as kbc
    try:
        with kbc.connect_closing(path) as conn:
            row = conn.execute("SELECT gateway_id FROM kanban_sync_state LIMIT 1").fetchone()
        if row and row[0]:
            return str(row[0])
        from hermes_cli import kanban_db_sync as sync
        return str(sync.sync_config().get("gateway") or "?")
    except Exception:  # health: allow BLE001 -- a name for the card, never a reason to skip it
        return "?"


def _detail(slug: str, path: Path, seen: _Seen, elapsed: float, rnd: Optional[_Round], now: float,
            wall_now: float) -> str:
    from hermes_cli import kanban_sync_runstate as runstate
    stamp = (f"replica last at the feed head {_pt(seen.stamp)}" if seen.stamp is not None
             else "no readable replica (sync-shadow) on disk")
    parts = [f"host {socket.gethostname()} (gateway {_gateway(path)}), board `{slug}`: {stamp}",
             f"not at the feed head for {elapsed / 60:.0f} awake min (bound {REPLICA_STALE_S / 60:.0f})",
             _outcome(rnd, now, wall_now - elapsed)]
    down = runstate._read(runstate.path())
    if down and down.get("running") is False:
        parts.append(runstate.describe(down))
    pending = _pending(path)
    parts.append(f"pending outbox ops: {'?' if pending is None else pending}")
    parts.append(f"core {_core_sha() or '?'}")
    return "; ".join(parts)


def frozen_children(path: Path) -> list[tuple[str, str, str, Optional[str]]]:
    """``(child, child status, remote parent, mirrored status)`` for every local card waiting on a parent that is
    not a local card: nothing promotes it while this host's sync is stopped (stuck-card-recovery.md item 3)."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_sync as sync
    with kbc.connect_closing(path) as conn:
        mirror = sync.has_remote_parents(conn)
        sql = ("SELECT c.id, c.status, l.parent_id, " + ("m.status" if mirror else "NULL") +
               " FROM task_links l JOIN tasks c ON c.id = l.child_id LEFT JOIN tasks p ON p.id = l.parent_id " +
               (f"LEFT JOIN {sync.REMOTE_PARENTS_TABLE} m ON m.parent_id = l.parent_id " if mirror else "") +
               "WHERE p.id IS NULL AND c.status IN ('todo', 'blocked') ORDER BY c.id, l.parent_id LIMIT ?")
        return [tuple(r) for r in conn.execute(sql, (_FROZEN_CAP,))]


def _board_status(board: str, ids: list[str]) -> dict[str, str]:
    """Each parent's status on the Board itself (never the stale replica), within a 15 s total budget."""
    from hermes_cli import kanban_sync_client as ksc
    hub = ksc.Hub(ksc.load_identity(), timeout=5.0, deadline=time.monotonic() + 15.0)
    out: dict[str, str] = {}
    try:
        for pid in ids:
            row = (hub.row_evidence(board, "tasks", pid) or {}).get("row")
            if isinstance(row, dict) and row.get("status"):
                out[pid] = str(row["status"])
    finally:
        hub.close()
    return out


def _body(slug: str, path: Path, detail: str, board_status) -> str:
    frozen: list = []
    try:
        frozen = frozen_children(path)
    except (sqlite3.Error, OSError):
        log.debug("kanban sync: cannot list frozen children on %s", slug, exc_info=True)
    lines = []
    if frozen:
        try:
            on_board: Optional[dict[str, str]] = board_status(slug, sorted({p for _c, _s, p, _m in frozen}))
        except Exception:  # health: allow BLE001 -- an unreachable Board falls back to the mirror
            on_board = None
        for child, status, parent, mirrored in frozen:
            if on_board is not None and parent in on_board:
                where = f"Board status: {on_board[parent]}"
            else:
                where = f"{mirrored or 'unknown'} (local mirror, may be stale)"
            lines.append(f"- `{child}` ({status}) waits on remote parent `{parent}`: {where}")
    frozen_text = ("Cards homed here that wait on a remote parent (nothing promotes them while this replica is "
                   "stale; after 10 min the home may block them parent_missing). Re-place each card's next step on "
                   "a worker, following kanban-orchestrator references/stuck-card-recovery.md item 3 (precedent: "
                   "t_01a1d42acdce8e23):\n" + "\n".join(lines) if lines else
                   "No card homed here waits on a remote parent.")
    return (
        "This host's replica of a synced board has not reached the Board's feed head for over 20 awake minutes, "
        "so remote parents, missing-parent checks and ready notices read an old copy (and since #1931 they wait "
        "silently instead of misfiring).\n\n"
        f"{detail}\n\n{frozen_text}\n\n"
        "Check: `hermes kanban sync status` (cursor, pending, last_error) and agent.log `kanban sync:` lines; "
        "`.sync-state.json` beside the board records a loop that gave up. Done when the replica reaches the head "
        "again and the cause is fixed (precedents: t_01a1d2cc49634a78, t_01a1d36ddcea59fe)."
    )


def report(slug: str, detail: str, body: str) -> Optional[str]:
    """Open or comment the stale-replica card on the default board. Best effort: never raises."""
    from hermes_cli import kanban_sync_outbox_age as outbox_age
    return outbox_age.report(slug, detail, key=KEY, author=AUTHOR, body=body, what="replica stale",
                             title=f"Kanban sync: {slug} replica has not reached the feed head for 20+ awake min")


def reset() -> None:
    """Tests: forget every observation."""
    with _LOCK:
        _SEEN.clear()
        _ROUNDS.clear()
    _TELEMETRY_AT.clear()
