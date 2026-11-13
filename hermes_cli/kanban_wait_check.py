"""``hermes kanban wait-check``: the one supported cross-gateway wait probe.

Exit 0 = condition met, 1 = not yet, 2 = cannot tell (unknown card, unreachable gateway,
unreadable DB). The wake-check scheduler treats 2 as broken (3 in a row wakes the card),
never as "not yet". 3 = unknown, not yet: the gateway is unreachable and its hermes-hub replica,
which still holds the card, is stale (a sleeping Mac). The scheduler re-probes 3 without counting it
as broken until the replica has been stale for ``kanban_wake_check.STALE_WAKE_AFTER``. Arguments
travel as argv (``shlex.join`` for ssh), so SQL/quoting never gets re-split by a remote shell.

    hermes kanban wait-check task t_x [--gateway hub] [--board SLUG] [--until done,review,blocked,triage,scheduled,archived]
                                     [--from running]
    hermes kanban wait-check board-empty SLUG [--gateway hub]
"""
from __future__ import annotations

from hermes_cli import kanban_lockguard as _kanban_lockguard
import json
import os
import shlex
import sqlite3
import subprocess
import time
import sys
from pathlib import Path
from typing import Optional

# [daveey] The default is a tracker's wake: the owner finished, wants review, blocked, went to triage, or parked on
# something only an agent can do (``parked-agent``: a scheduled card whose wait has no live producer, per
# kanban_wait_info). A narrower list (done/review/triage) left a chat asleep for hours while its remote owner sat
# parked. Plain ``scheduled`` is not in the default: continuation checkpoints park a running card ``scheduled``
# between runs, and a tracker woken by each of them would loop. An older remote that does not know
# ``parked-agent`` just never matches it (exactly the old behaviour).
PARKED_AGENT = "parked-agent"
DEFAULT_UNTIL = ("done", "review", "blocked", "triage", PARKED_AGENT, "archived")
GATEWAY_SSH = {"hub": "ubuntu@100.104.201.88"}
SSH_TIMEOUT = 25


def _db(board: Optional[str]) -> Path:
    if board is not None:
        from hermes_cli.kanban_db import board_own_db_path
        return board_own_db_path(board)
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    if os.environ.get("HERMES_KANBAN_DB"):
        return Path(os.environ["HERMES_KANBAN_DB"])
    return home / "kanban.db"


def _query(path: Path, sql: str, args=()):
    last: Optional[Exception] = None
    for _ in range(3):  # WAL/lock flakes are transient
        try:
            c = _kanban_lockguard.connect(f"file:{path}?mode=ro", uri=True, timeout=20)
            try:
                return c.execute(sql, args).fetchall()
            finally:
                c.close()
        except sqlite3.OperationalError as exc:
            last = exc
    raise last  # type: ignore[misc]


def _task_source(board, target):
    """Live rows win; evicted worker copies remain in the selected Board shadow.

    This is not the offline gateway cache in kanban_wait_replica. Unreadable
    databases remain errors, not absence.
    """
    live = _db(board)
    from hermes_cli.kanban_db_sync import board_slug_for_path
    from hermes_cli.kanban_sync_client import shadow_path
    slug = board_slug_for_path(str(live))
    shadow = shadow_path(slug)
    for path in (live, shadow):
        if path.exists():
            rows = _query(path, "select status from tasks where id=?", (target,))
            if rows:
                return path, rows
    return live, []


def check_local(kind: str, target: str, *, board: Optional[str] = None, until=DEFAULT_UNTIL,
                since=()) -> tuple[int, str]:
    """``since``: statuses the target had when the waiter parked; still in one of them = not yet (exit 1), so a
    tracker that re-parks after handling a wake waits for the NEXT change instead of waking again at once."""
    try:
        if kind == "task":
            source, rows = _task_source(board, target)
            if not rows:
                return 2, f"error: {target} not found"
            return _verdict(board, target, rows[0][0], source=source, until=until, since=since)
        if kind == "board-empty":
            n = _query(_db(target), "select count(*) from tasks where status not in ('done','archived')")[0][0]
            return (0 if n == 0 else 1), f"{target} open={n}"
    except Exception as exc:  # any failure is "cannot tell", never "not yet"
        return 2, f"error: {exc}"
    return 2, f"error: unknown check {kind!r}"


def _verdict(board, target, status, *, source, until, since, note="") -> tuple[int, str]:
    """``check_local``'s judgement of one observed ``status`` (``note``: a source tag after the status)."""
    if status in set(since or ()):
        return 1, f"{target} {status} (unchanged since park){note}"
    suffix = _producer_suffix(board, target, source=source) if status in ("scheduled", "blocked") else ""
    if status in set(until):
        return 0, f"{target} {status}{suffix}{note}"
    if PARKED_AGENT in set(until) and status in ("scheduled", "blocked") and _agent_owned_park(board, target, source=source):
        return 0, f"{target} {status} (agent-owned park: nothing else will produce what it waits for){note}"
    return 1, f"{target} {status}{suffix}{note}"


def _board_replica_task(board, target, *, until, since):
    """``(rc, message)`` for ``target`` as this gateway's Board sync replica (``sync-shadow``) holds it,
    or None when that replica cannot vouch for it: no replica or row, unreadable, or not pulled to the feed head
    within ``kanban_replica_freshness.FRESH_SECONDS`` (sync stopped or detached). Never raises."""
    from hermes_cli.kanban_replica_freshness import FRESH_SECONDS
    from hermes_cli.kanban_sync_client import shadow_path
    try:
        path = shadow_path(board or "default")
        if not path.exists():
            return None
        state = _query(path, "select pulled_at from kanban_sync_state limit 1")
        rows = _query(path, "select status from tasks where id=?", (target,))
        pulled = state[0][0] if state else None
        if not rows or not isinstance(pulled, (int, float)) or not 0 <= time.time() - pulled <= FRESH_SECONDS:
            return None
        return _verdict(board, target, rows[0][0], source=path, until=until, since=since, note=" (board replica)")
    except Exception:  # health: allow BLE001 -- an unreadable Board replica is no evidence; the registry answer stands
        return None


def _unreachable_task(gateway, target, *, board, until, since, live_timeout) -> tuple[int, str]:
    """The owner gateway cannot be asked (no route, or a forced-command key). Two local copies may answer: the
    hermes-hub registry replica (collector-refreshed, owner-qualified) and this gateway's Board sync replica (the
    same Board row the owner's own ``wait-check`` falls back to, applied each sync round). The Board replica runs
    minutes ahead, and it is the one whose apply expedites the park (``kanban_awaited_card_wake``): reading only
    the registry, the expedited probe saw the old status and slept a full ``check_every`` (t_01a1d5ead059278d,
    0 of 2 live parks woke). So a fresh Board replica may only move the answer to met (rc 0): it never downgrades
    the registry's met, nor replaces its rc 3 (stale or unknown scope: the owner may be offline, and its frozen
    Board row would hide the gateway_offline escalation) with a not-yet. Its ``pulled_at`` is this gateway's pull
    time, not when the owner wrote the row, so it is not weighed against the registry's ``observed_at``. A live
    owner read stands; a Board replica that is not fresh adds nothing."""
    from hermes_cli.kanban_wait_replica import check
    rc, msg = check(gateway, target, board=board, until=until, since=since, live_timeout=live_timeout)
    if rc == 0 or "(live owner" in msg:
        return rc, msg
    board_view = _board_replica_task(board, target, until=until, since=since)
    return board_view if board_view is not None and board_view[0] == 0 else (rc, msg)


def _producer_suffix(board, target, *, source=None):
    from hermes_cli.kanban_session_activity import _open_ro
    from hermes_cli.kanban_awaiting import card_orphan_reason
    conn = _open_ro(source or _db(board))
    try:
        return " (orphan producer)" if card_orphan_reason(conn, target) else ""
    finally:
        conn.close()


def _agent_owned_park(board: Optional[str], target: str, *, source=None) -> bool:
    """The target parked on something only an agent can end (certain, or a legacy park that has stalled)."""
    from hermes_cli.kanban_session_activity import _local_wait_info, _open_ro

    conn = _open_ro(source or _db(board))
    try:
        info = _local_wait_info(conn, [target]).get(target) or {}
    finally:
        conn.close()
    return bool(info.get("agent_owned") and (info.get("agent_owned_certain") or info.get("wait_state") == "stalled"))


def check_remote(gateway: str, kind: str, target: str, *, board: Optional[str] = None, until=DEFAULT_UNTIL,
                 since=()) -> tuple[int, str]:
    started = time.monotonic()
    inner = ["hermes", "kanban", "wait-check", kind, target, "--until", ",".join(until)]
    # ``since`` (--from) is applied here, on the reply, so an older remote never sees a flag it would reject.
    if board:
        inner += ["--board", board]
    host = GATEWAY_SSH.get(gateway, gateway)
    remote = "export PATH=$HOME/.local/bin:$HOME/.hermes/bin:$PATH; " + shlex.join(inner)
    try:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_TIMEOUT}", host, remote],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=15 if kind == "task" else SSH_TIMEOUT + 15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if kind == "task":
            return _unreachable_task(gateway, target, board=board, until=until, since=since,
                                     live_timeout=25 - (time.monotonic() - started))
        return 2, f"error: ssh {host}: {exc}"
    # A forced-command key can reach SSH but cannot execute the owner CLI.
    # Treat only its explicit refusal as unavailable transport, not arbitrary
    # rc2 owner errors. The existing replica reader preserves gateway/board
    # identity, freshness (rc3), missing/error (rc2), and orphan evidence; it
    # never upgrades a cached observation to a live authoritative read.
    restricted = p.returncode == 2 and p.stderr.strip().startswith("hub-read: refused:")
    if (p.returncode == 255 or restricted) and kind == "task":
        return _unreachable_task(gateway, target, board=board, until=until, since=since,
                                 live_timeout=25 - (time.monotonic() - started))
    out = (p.stdout + p.stderr).strip()
    if p.returncode == 0 and since and kind == "task":
        words = (p.stdout or "").split()
        if len(words) >= 2 and words[0] == target and words[1] in set(since):
            return 1, f"{out} (unchanged since park)"
    if p.returncode in (0, 1, 3):  # 3: the remote read a stale replica of a third gateway
        return p.returncode, out
    return 2, f"error: remote wait-check on {host} exited {p.returncode}: {out[-300:]}"


def handled_status(waiter: Optional[str], db_path: Optional[str], target: str) -> Optional[str]:
    """The target status this probe's waiter already woke for: the newest ``condition_met`` on the waiter whose probe
    names ``target`` and whose output read ``<target> <status>``. A tracker that handled that wake and re-parked
    with the same probe must not be met again while the target still sits in that status (review or triage can
    last hours; each probe run would wake it again). Read from the probe env the dispatcher sets
    (``HERMES_KANBAN_TASK`` = the waiter, ``HERMES_KANBAN_DB`` = its board); None outside a probe or on any error."""
    if not (waiter and db_path and target):
        return None
    try:
        rows = _query(Path(db_path), "select payload from task_events where task_id=? and kind='condition_met' "
                                     "order by id desc limit 5", (waiter,))
    except Exception:
        return None
    for (raw,) in rows:
        try:
            payload = json.loads(raw or "{}")
        except ValueError:
            continue
        if target not in str(payload.get("wake_check") or ""):
            continue
        for line in str(payload.get("output") or "").splitlines():
            words = line.split()
            if len(words) >= 2 and words[0] == target:
                return words[1]
        return None
    return None


def run(args) -> int:
    until = tuple(u for u in (args.until or "").split(",") if u) or DEFAULT_UNTIL
    since = tuple(u for u in (getattr(args, "since", None) or "").split(",") if u)
    if not since and args.check == "task" and os.environ.get("HERMES_KANBAN_WAKE_PROBE") == "1":
        # Implicit --from, for dispatcher probe runs only (a worker running wait-check by hand sees the plain
        # status): the status the waiter already handled (see handled_status).
        seen = handled_status(os.environ.get("HERMES_KANBAN_TASK"), os.environ.get("HERMES_KANBAN_DB"), args.target)
        since = (seen,) if seen else ()
    gw = getattr(args, "gateway", None)
    if args.check == "capability-owner":
        # Owner cards live on another gateway; the hermes-hub replica on THIS gateway is the
        # source (request result -> owner task id -> replicated status). [daveey]
        from hermes_cli.kanban_capability_routing import wait_check
        rc, msg = wait_check(args.target)
        print(msg, file=sys.stderr if rc == 2 else sys.stdout)
        return rc
    if gw in (None, "", "local"):
        rc, msg = check_local(args.check, args.target, board=args.board, until=until, since=since)
    else:
        rc, msg = check_remote(gw, args.check, args.target, board=args.board, until=until, since=since)
    print(msg, file=sys.stderr if rc == 2 else sys.stdout)
    return rc
