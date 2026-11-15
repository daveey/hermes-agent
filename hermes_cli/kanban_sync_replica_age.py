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
path (comment at most hourly on an open card, a new card once it is done). Monotonic time stops in sleep but runs
in macOS DarkWake. Each check samples cron.restart_rerun.host_fully_awake once: when False, the interval since
each board's previous observation is excluded and no card is filed. This sampled rule also excludes an idle
lid-closed Mac on AC with an external display, and an idle lid-open Mac on battery; counting resumes when someone
is active. Non-macOS and probe failures read awake. The incident ends when the stamp advances.

The observation is also kept in ``.replica-age.json`` beside ``.sync-state.json`` (with the boot id), so a gateway
restart resumes the same awake clock and open incident: on 2026-11-13 the hub restarted its gateway 148 times
(median 4.2 min apart) and an in-memory clock never reached the bound (t_01a1d5ead18cf1c3). Monotonic time is shared
by every process of one boot. The last observation and excluded seconds persist too, so a same-boot restart
continues excluding DarkWake; another boot, or a span of over :data:`REPLICA_STALE_S` between processes with no
observation, starts afresh. Old records resume with last=seen_at and excluded=0.

One check covers every shape: rounds ending behind, rounds failing, no rounds at all (a dead loop or a give-up),
and a missing replica. The card names the host, board, stamp (PT), awake minutes, the last round's outcome, the
loop's recorded stop, the pending outbox, the running core and the cards frozen behind a remote parent.
"""
from __future__ import annotations

import json
import logging
import math
import os
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
_MEMO = ".replica-age.json"
_FROZEN_CAP = 25
_PT = "America/Los_Angeles"


@dataclass
class _Seen:
    stamp: Optional[float]  # the replica's last pulled_at (None: no readable replica yet)
    since: float  # monotonic time the stamp was first seen (or the first observation)
    open: bool = False  # an incident is open (its card filed)
    last: float = 0.0  # monotonic time of the previous observation
    excluded: float = 0.0  # observed time not fully awake


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
          board_status: Optional[Callable[[str, list[str]], dict[str, str]]] = None,
          awake_fn: Optional[Callable[[], bool]] = None) -> list[str]:
    """One observation of every board (``now``: monotonic). Returns the card ids it filed or commented. Never
    raises."""
    try:
        if awake_fn is None:
            from cron.restart_rerun import host_fully_awake
            awake_fn = host_fully_awake
        awake = awake_fn()
    except Exception:  # health: allow BLE001 -- a broken probe must not suppress a real stall
        awake = True
    out: list[str] = []
    for slug, path in boards:
        try:
            tid = _check_board(slug, path, now, wall, file_fn or report, board_status or _board_status, awake)
            if tid:
                out.append(tid)
        except Exception:  # health: allow BLE001 -- an alert must never break the dispatcher tick
            log.warning("kanban sync: replica freshness check failed for %s", slug, exc_info=True)
    return out


def _boot_id() -> Optional[str]:
    """This boot's identity (monotonic time is comparable across processes only within one boot), else None."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip() or None
    except OSError:
        pass
    try:  # macOS: kern.bootsessionuuid
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        buf = ctypes.create_string_buffer(64)
        size = ctypes.c_size_t(len(buf))
        if libc.sysctlbyname(b"kern.bootsessionuuid", buf, ctypes.byref(size), None, ctypes.c_size_t(0)) == 0:
            return buf.value.decode("ascii", "replace") or None
    except (OSError, AttributeError, TypeError, ValueError):
        pass
    return None


def _memo_path() -> Path:
    """Beside ``.sync-state.json``: each board's observation, so a gateway restart resumes the awake clock."""
    from hermes_cli import kanban_db as kb
    return kb.kanban_home() / "kanban" / _MEMO


def _load_memo() -> dict[str, Any]:
    try:
        memo = json.loads(_memo_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return memo if isinstance(memo, dict) else {}


def _save_memo(update: Callable[[dict[str, Any]], None]) -> None:
    target = _memo_path()
    try:
        memo = _load_memo()
        update(memo)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{_MEMO}.{os.getpid()}.{threading.get_ident()}.tmp")
        temp.write_text(json.dumps(memo), encoding="utf-8")
        temp.replace(target)
    except OSError:
        log.debug("kanban sync: cannot record the replica observation at %s", target, exc_info=True)


def _resume(slug: str, now: float) -> Optional[_Seen]:
    """The previous process's observation of ``slug``, when it is safe to keep counting from it: the same boot
    (monotonic time stops in sleep on Linux and macOS and is shared by every process of one boot), not in the
    future, and observed within :data:`REPLICA_STALE_S` (a longer unwatched span says nothing about the replica)."""
    rec = _load_memo().get(slug)
    boot = _boot_id()
    if not isinstance(rec, dict) or boot is None or rec.get("boot") != boot:
        return None
    try:
        stamp = None if rec.get("stamp") is None else float(rec["stamp"])
        since, seen_at = float(rec["since"]), float(rec["seen_at"])
        last = float(rec.get("last", seen_at))
        excluded = float(rec.get("excluded", 0))
    except (KeyError, TypeError, ValueError):
        return None
    if (not since <= last <= seen_at <= now or now - seen_at > REPLICA_STALE_S
            or not math.isfinite(excluded) or excluded < 0):
        return None
    return _Seen(stamp, since, bool(rec.get("open")), last, excluded)


def _remember(slug: str, seen: Optional[_Seen], now: float) -> None:
    boot = _boot_id()

    def update(memo: dict[str, Any]) -> None:
        if seen is None or boot is None:
            memo.pop(slug, None)
        else:
            memo[slug] = {"stamp": seen.stamp, "since": seen.since, "open": seen.open, "seen_at": now, "boot": boot,
                          "last": seen.last, "excluded": seen.excluded}

    _save_memo(update)


def _check_board(slug: str, path: Path, now: float, wall: Callable[[], float], file_fn, board_status,
                 awake: bool = True) -> Optional[str]:
    if _mode(path) not in ("primary", "shadow"):
        with _LOCK:
            known = _SEEN.pop(slug, None)  # detached or unsynced: nothing to judge, and a later attach starts afresh
        if known is not None or slug in _load_memo():
            _remember(slug, None, now)
        return None
    stamp = _stamp(slug)
    with _LOCK:
        seen = _SEEN.get(slug)
        if seen is None:
            seen = _resume(slug, now)  # a restarted gateway keeps counting the same incident
            if seen is not None:
                _SEEN[slug] = seen
        # Any change counts: ``pulled_at`` is wall time, and a wall clock stepped back (NTP, a manual fix) writes
        # stamps below the remembered one on healthy rounds.
        if seen is None or (stamp is not None and (seen.stamp is None or stamp != seen.stamp)):
            if seen is not None and seen.open:
                log.info("kanban sync: %s replica reached the feed head again at %s after %.0f awake min; "
                         "incident over", slug, _pt(stamp), (now - seen.since) / 60)
            seen = _SEEN[slug] = _Seen(stamp, now, last=now)
        if not awake:
            delta = max(0.0, now - seen.last)
            seen.since += delta
            seen.excluded += delta
        seen.last = now
        elapsed = now - seen.since
        due = awake and elapsed >= REPLICA_STALE_S and not seen.open
        if due:
            seen.open = True
        rnd = _ROUNDS.get(slug)
    _remember(slug, seen, now)
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
             "incident %s, excluded %.0f s not fully awake", slug, _pt(seen.stamp) if seen.stamp else "none",
             elapsed, REPLICA_STALE_S, "open" if seen.open else "none", seen.excluded)


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
             f"not at the feed head for {elapsed / 60:.0f} awake min (bound {REPLICA_STALE_S / 60:.0f}), "
             f"excluded {seen.excluded:.0f} s not fully awake",
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
