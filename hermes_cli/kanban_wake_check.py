"""Condition wakes: a ``scheduled`` card wakes when a cheap shell probe passes.

A timed park (``kanban_schedule``, ``kanban_block`` kind=time, ``kanban_continue`` with a
wait) may carry ``wake_check`` (a shell command) and ``check_every`` (seconds). The
dispatcher probes it off-thread on each tick:

* exit 0 -> the condition holds: the card wakes now (parents re-gated, ``resume_status``
  honoured, like ``wake_due_continuations``) with a ``condition_met`` event carrying the
  stdout tail, which the next worker's context shows;
* exit 1 -> not yet: probe again ``check_every`` seconds later;
* exit 3 (``UNKNOWN_RC``) with a ``stale_age=<N>s`` line -> unknown, not yet: ``hermes kanban
  wait-check`` read the card from a stale replica of an offline gateway (a sleeping Mac). Probed
  again without counting toward ``BROKEN_AFTER``, until the replica is ``STALE_WAKE_AFTER`` old;
  then the card wakes (``wake_check_broken`` with ``reason=gateway_offline``) saying how long the
  gateway has been offline. Identical task/probe/gateway escalations are limited to once per
  24 hours across worker reparks; regular probes and successful-condition wakes are unchanged;
* exit 3 with ``source_state=unknown`` and ``gateway=<name>`` -> unknown, not yet: the producer's gateway
  published that board's scope as ``unknown`` (its collector could not read the board), so nothing vouches
  for the last-seen status and there is no observation age. Never counted toward ``BROKEN_AFTER``; the
  first such result wakes the card once (``wake_check_broken`` with ``reason=gateway_scope_unknown``), and
  later ones for the same task/probe/gateway are held while a source-unavailable escalation
  (``gateway_offline`` or ``gateway_scope_unknown``) is under 24 hours old;
* anything else, or a timeout, ``BROKEN_AFTER`` times in a row -> the card wakes with a
  ``wake_check_broken`` event so a worker fixes the probe. A broken probe is never silent.

The park's ``wake_at`` stays the deadline backstop. Probe state lives in one row per card
(``task_wake_checks``), not one event per probe.

The tick never waits on a probe: due probes go to a module-level pool (``MAX_CONCURRENT``
workers), and each probe thread applies its own result through its own connection, so a
slow probe cannot stall dispatch and a one-shot dispatcher process still records results.

``guard_error`` is the park guard: a park that wakes more than ``GUARD_SECONDS`` away with
no ``wake_check`` is refused unless the caller says ``clock_only`` (a pure time wait).
Without it workers parked 12 h behind a clock to save LLM runs while the condition they
waited on was already true (t_9be6b361). [daveey]
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_wake_episode as episode

logger = logging.getLogger(__name__)

DEFAULT_CHECK_EVERY = 900
MIN_CHECK_EVERY = 120
MAX_CHECK_EVERY = 21600
PROBE_TIMEOUT_SECONDS = 30
MAX_CONCURRENT = 4
# ``run_due_wake_checks(wait=True)`` never waits longer than this; the dispatcher never waits.
TICK_BUDGET_SECONDS = 60
BROKEN_AFTER = 3
# "Unknown, not yet" (kanban_wait_replica.STALE_RC): the producer's gateway is offline and its replica stale.
UNKNOWN_RC = 3
STALE_WAKE_AFTER = 24 * 3600
STALE_AGE_RE = re.compile(r"\bstale_age=(\d+)s\b")
STALE_GATEWAY_RE = re.compile(r"\bgateway=(\S+)")
# kanban_wait_replica.unknown_message: the owner published this board's scope as ``unknown``. [daveey]
SOURCE_UNKNOWN_RE = re.compile(r"\bsource_state=unknown\b")
SCOPE_UNKNOWN_REASON = "gateway_scope_unknown"
# Escalations meaning "the producer's owner cannot vouch for its card": one acknowledges the other.
SOURCE_UNAVAILABLE_REASONS = ("gateway_offline", SCOPE_UNKNOWN_REASON)
GUARD_SECONDS = 2 * 3600
OUTPUT_TAIL_CHARS = 500
MAX_CMD_CHARS = 2000
CONDITION_MET_EVENT = "condition_met"
BROKEN_EVENT = "wake_check_broken"
# [daveey] Probe placement on synced boards (``task_wake_checks`` is machine-local and never syncs). The probe runs
# on the gateway that parked the card (``probe_host`` on the park event): that is where the park's dry run passed
# and where host-only state lives (a ``wait-check capability-owner`` request id exists only in the store of the
# gateway that opened it). A worker node keeps its scheduled copy of another gateway's card while the probe is live
# (``kanban_worker_node.evict``); a wake there is a status write the Board carries home. The home board re-registers
# a probe it lost (``RESTORED_EVENT``, once), and wakes the card with a comment when a probe is lost again or a
# relayed park's probe host has gone silent (``LOST_EVENT``), so a lost probe never sleeps silently until the clock.
RESTORED_EVENT = "wake_check_restored"
LOST_EVENT = "wake_check_lost"
ALIVE_EVENT = "wake_check_alive"
# A worker node's probe on another gateway's card says it is alive at most this often (one synced event).
ALIVE_EVERY = 3 * 3600
SILENT_GRACE = 900
_RESTORE_SEEN: dict[tuple[str, str], int] = {}
# Output that shows a probe crashed even though it exited 1 ("not yet"): a traceback, a
# shell syntax/lookup error, a sqlite failure, or a script's own ``error:`` line. Such a
# probe can never succeed, so it counts toward BROKEN_AFTER instead of looking like waiting.
ERROR_OUTPUT_RE = re.compile(
    r"Traceback \(most recent call last\)|^\s*(?:\S+: )?(?:line \d+: )?.*(?:syntax error|command not found"
    r"|No such file or directory|unexpected EOF)|OperationalError|database is locked"
    r"|unable to open database file|^\s*error:", re.IGNORECASE | re.MULTILINE)


def stale_age(rc: Any, out: str) -> Optional[int]:
    """Seconds the producer's replica has been stale when this result is "unknown, not yet", else None."""
    if rc != UNKNOWN_RC or output_error(out):
        return None
    m = STALE_AGE_RE.search(out or "")
    return int(m.group(1)) if m else None


def source_unknown(rc: Any, out: str) -> Optional[str]:
    """The producer's gateway when this result is "owner scope unknown, not yet", else None. [daveey]"""
    text = out or ""
    if rc != UNKNOWN_RC or output_error(text) or STALE_AGE_RE.search(text) or not SOURCE_UNKNOWN_RE.search(text):
        return None
    m = STALE_GATEWAY_RE.search(text)
    return (m.group(1).rstrip(",;)") or None) if m else None


def output_error(out: str) -> Optional[str]:
    """The matching error line when a probe's output shows it crashed, else None."""
    m = ERROR_OUTPUT_RE.search(out or "")
    return m.group(0).strip() or "error" if m else None


WAKE_CHECK_ALREADY_TRUE = "wake_check_already_true"


def refusal_code(message: str) -> Optional[str]:
    """The stable rejection code of a park-time probe refusal (``WAKE_CHECK_ALREADY_TRUE``), else None."""
    return WAKE_CHECK_ALREADY_TRUE if (message or "").startswith(WAKE_CHECK_ALREADY_TRUE) else None


def already_true_message(out: str, *, blocker_id: Any = None) -> str:
    """Refusal for a park whose wake_check already exits 0: it would wake at once. [daveey]"""
    msg = (f"{WAKE_CHECK_ALREADY_TRUE}: this wake_check already passes, so the park would wake at once: the "
           "condition is already true. Act on it now, or give a probe that tests what you are still waiting for.")
    if isinstance(blocker_id, str) and blocker_id.strip():
        msg += (f" It tests shared blocker {blocker_id.strip()!r}, so that wall is gone: carry on with the work, "
                "or park for your other reason with clear_blocker=true (that clears the blocker and wakes every "
                "card behind it).")
    return msg + f" Probe output: {_tail(out)!r}"


def dry_run_error(cmd: Optional[str], *, cwd: Optional[str] = None, env: Optional[dict] = None,
                  blocker_id: Any = None) -> Optional[str]:
    """Run ``cmd`` once at park time, before the write txn, with the dispatcher's runner and
    ``PROBE_TIMEOUT_SECONDS`` on this host (the one that registers and runs the park's probe, ``probe_host``).
    Refusal text when it can never succeed (rc >= 2, could not start, or exit 0/1 with error output), or
    when it already passes (exit 0: the park would wake at once, ``WAKE_CHECK_ALREADY_TRUE``), else None.
    A timeout is accepted: slow is not broken. ``HERMES_KANBAN_WAKE_DRYRUN=0`` skips it (tests).

    A worker's dry run carries the probe marker (``HERMES_KANBAN_WAKE_PROBE``) like the dispatcher's runs,
    so ``wait-check task`` applies the same implicit ``--from`` (a status this card already woke for reads
    "not yet") and the dry run agrees with the first real probe. [daveey]"""
    if cmd and re.search(r"\bhermes-hub\s+tasks\b", cmd) and re.search(r"\bt_[0-9a-f]{8,16}\b", cmd):
        return ("refused: hand-rolled replica task probes hide missing or stale cards. Use "
                "`hermes kanban wait-check task <id> --gateway <name> --until done,blocked` instead.")
    if not cmd or os.environ.get("HERMES_KANBAN_WAKE_DRYRUN") == "0":
        return None
    env = dict(env if env is not None else os.environ)
    if env.get("HERMES_KANBAN_TASK"):
        env["HERMES_KANBAN_WAKE_PROBE"] = "1"
    rc, out = _run_probe(("dry-run", cmd), cmd, cwd or str(Path.home()), env)
    if rc == "timeout":
        return None
    if rc == 0 and not output_error(out):
        return already_true_message(out, blocker_id=blocker_id)
    bad = None
    if stale_age(rc, out) is not None or source_unknown(rc, out):
        return None  # unknown, not yet: the producer's gateway is offline or cannot read its board right now
    if not isinstance(rc, int) or rc >= 2:
        bad = f"exited {rc}"
    elif output_error(out):
        bad = f"exit {rc} but its output shows an error ({output_error(out)})"
    if bad is None:
        return None
    return (f"refused: the wake_check was run once at park time and {bad}, so it could never "
            "succeed and would wait silently forever. Exit codes: 0 = condition holds, 1 = not "
            "yet, anything else = broken. For a wait on another card use "
            "`hermes kanban wait-check task <id> --gateway <name> --until done,blocked`; do not "
            f"hand-roll ssh+SQL (ssh re-splits quoted args). Output: {_tail(out)!r}")


GUARD_HELP = (
    "A wait needs a producer (awaiting={kind: card|ci|deploy|external|clock, ref, why}); a probe "
    "only says when to look. A condition only an agent action changes (a grant, a permission, an "
    "install, a fix) is work, not a wait: create its owner card where the capability lives and "
    "point awaiting at it. "
    "A wait on any readable fact needs wake_check=<shell command>: exit 0 = the condition "
    "holds (the card wakes at once), exit 1 = not yet (re-probed every check_every seconds, "
    "default 900, min 120). Examples: "
    "`sqlite3 ~/.bb/hub.db \"select build from devices where name='watch'\" | grep -q 559`, "
    "`gh api repos/OWNER/REPO/tags --jq '.[].name' | grep -q '^desktop-client-v'`, "
    "`hermes kanban wait-check task <id> --gateway <name> --until done,blocked`. "
    "A wait on another card on this board is a parent link (kanban_link), not a park. "
    "Only a pure time wait (a soak window, a calendar date, a timed re-measure) may pass "
    "clock_only=true; SCHEDULED_UNTIL stays the deadline either way."
)


# --- parameters + guard -------------------------------------------------------------------

def normalize_every(value: Any) -> int:
    """``check_every`` clamped to ``[MIN_CHECK_EVERY, MAX_CHECK_EVERY]``; None -> default."""
    if value is None or value == "":
        return DEFAULT_CHECK_EVERY
    try:
        every = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("check_every must be an integer number of seconds") from exc
    return max(MIN_CHECK_EVERY, min(every, MAX_CHECK_EVERY))


def normalize_cmd(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("wake_check must be a shell command string")
    cmd = value.strip()
    if len(cmd) > MAX_CMD_CHARS:
        raise ValueError(f"wake_check is longer than {MAX_CMD_CHARS} chars; put it in a script")
    return cmd or None


def guard_error(*, wake_at: Optional[int], wake_check: Optional[str], clock_only: bool,
                now: Optional[int] = None) -> Optional[str]:
    """Refusal text for a park waking more than ``GUARD_SECONDS`` away with no probe."""
    if wake_check or clock_only or wake_at is None:
        return None
    now = int(time.time()) if now is None else int(now)
    if wake_at - now <= GUARD_SECONDS:
        return None
    hours = (wake_at - now) / 3600
    return (f"refused: this park wakes in {hours:.1f}h and has no wake_check, so a condition "
            f"that comes true early would sit unnoticed until then. {GUARD_HELP}")


def validate_park(*, wake_at: Optional[int], wake_check: Optional[str],
                  check_every: Optional[int], clock_only: bool) -> tuple[Optional[str], int]:
    """Shared transition invariant, checked before writes, not just by UI wrappers."""
    cmd = normalize_cmd(wake_check)
    every = normalize_every(check_every)
    error = guard_error(wake_at=wake_at, wake_check=cmd, clock_only=clock_only)
    if error:
        raise ValueError(error)
    return cmd, every


def planned_wake(conn: sqlite3.Connection, task_id: str, *, op: str, reason: Optional[str] = None,
                 kind: Optional[str] = None, blocker_id: Optional[str] = None,
                 human_request: Any = None, wake_check: Optional[str] = None, clock_only: bool = False,
                 resume_after: int = 0, now: Optional[int] = None) -> Optional[int]:
    """The wake time a park call would set, or None when the call is not a timed park
    (``op`` is ``schedule``, ``block`` or ``continue``)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_timed_park as ktp

    now = int(time.time()) if now is None else int(now)
    if op == "schedule":
        return ktp.scheduled_payload(reason, now=now)["wake_at"]
    if op == "block":
        from hermes_cli.kanban_shared_blockers import normalize_blocker_id
        mode = ktp.park_mode(kind, reason, parents_satisfied=kb._parents_satisfied(conn, task_id),
                             blocker_id=normalize_blocker_id(blocker_id), human_request=human_request,
                             wake_check=wake_check, clock_only=clock_only)
        if mode == "timed":
            return ktp.scheduled_until(reason, now=now) or now + ktp.DEFAULT_WAKE_SECONDS
        if mode == "blocker":
            return now + ktp.blocker_wake_seconds()
        if kb._probe_parks(kind, wake_check, clock_only, human_request):
            return ktp.scheduled_until(reason, now=now) or now + ktp.DEFAULT_WAKE_SECONDS
        return None
    if op == "continue":
        return now + int(resume_after) if resume_after and int(resume_after) > 0 else None
    raise ValueError(f"unknown park op {op!r}")


def park_fields(wake_check: Optional[str], check_every: Optional[int], clock_only: bool,
                *, conn: Optional[sqlite3.Connection] = None, probe_host: Optional[str] = None) -> dict:
    """Keys added to the parking event payload (only the ones that are set). With ``conn`` on a synced board,
    ``probe_host`` names the gateway that runs the probe: this one, unless the caller hands it to another
    (``probe_host``, e.g. a capability relay to the card's home). [daveey]"""
    out: dict = {}
    if wake_check:
        out["wake_check"] = wake_check
        out["check_every"] = normalize_every(check_every)
        host = probe_host or own_gateway(conn)
        if host:
            out["probe_host"] = host
    if clock_only:
        out["clock_only"] = True
    return out


# --- probe placement (synced boards) --------------------------------------------------------

def own_gateway(conn: Optional[sqlite3.Connection]) -> Optional[str]:
    """This gateway's id on a board that syncs to the Board service, else None."""
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT gateway_id FROM kanban_sync_state LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row[0]) if row and row[0] else None


def probe_here(conn: sqlite3.Connection, probe_host: Optional[str]) -> bool:
    """Whether this gateway registers a park's probe (False only when ``probe_host`` names another gateway)."""
    if not probe_host:
        return True
    own = own_gateway(conn)
    return not own or own == probe_host


def foreign_ids(conn: sqlite3.Connection, own: Optional[str] = None) -> set[str]:
    """Ids of cards in this live file that another gateway created (worker-node copies)."""
    own = own or own_gateway(conn)
    if not own:
        return set()
    try:
        rows = conn.execute("SELECT id FROM tasks WHERE created_on IS NOT NULL AND created_on != ?", (own,))
        return {r[0] for r in rows.fetchall()}
    except sqlite3.OperationalError:
        return set()


def foreign_copy(conn: sqlite3.Connection, task_id: str) -> bool:
    own = own_gateway(conn)
    if not own:
        return False
    try:
        row = conn.execute("SELECT created_on FROM tasks WHERE id = ?", (task_id,)).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and row[0] and row[0] != own)


def _note_alive(conn: sqlite3.Connection, task_id: str, cmd: str, now: int) -> None:
    """On a worker node's copy, say (at most every ``ALIVE_EVERY``) that the relayed probe still runs, so the
    home board can tell a live remote probe from a lost one. In the caller's txn."""
    from hermes_cli import kanban_continuation as kc
    from hermes_cli import kanban_db as kb

    if not foreign_copy(conn, task_id):
        return
    kinds = (ALIVE_EVENT, *kc.SCHEDULED_ENTRY_EVENTS)
    marks = ",".join("?" for _ in kinds)
    last = conn.execute(f"SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind IN ({marks})",
                        (task_id, *kinds)).fetchone()[0] or 0
    if now - int(last) >= ALIVE_EVERY:
        kb._append_event(conn, task_id, ALIVE_EVENT, {"wake_check": cmd, "host": own_gateway(conn)})


def _lost_wake(conn: sqlite3.Connection, task_id: str, payload: dict, note: str) -> Optional[str]:
    from hermes_cli import kanban_db as kb

    woke = _wake(conn, task_id, LOST_EVENT, {**payload, "note": note})
    if woke:
        kb.add_comment(conn, task_id, "kanban-dispatcher", note)
    return woke


def restore_missing_checks(conn: sqlite3.Connection, *, now: Optional[int] = None) -> dict:
    """Lint every ``scheduled`` card whose latest park carries a wake_check but has no probe on this board.

    * The park ran here (``probe_host`` is this gateway, or unset on a card this gateway owns): re-register the
      probe once (``RESTORED_EVENT``); a second loss of the same park wakes the card with a comment.
    * The park was relayed from another gateway (a worker node ran the card): that host runs the probe and says
      so with ``ALIVE_EVENT``; when it has been silent past ``ALIVE_EVERY`` + two intervals, wake the card with a
      comment instead of letting it sleep until its clock.
    A worker node's copy of another gateway's card is left to its home board. Returns ids restored and woken."""
    from hermes_cli import kanban_continuation as kc
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    out: dict = {"restored": [], "woken": []}
    now = int(time.time()) if now is None else int(now)
    try:
        missing = [r[0] for r in conn.execute(
            "SELECT t.id FROM tasks t LEFT JOIN task_wake_checks w ON w.task_id = t.id "
            "WHERE t.status = 'scheduled' AND w.task_id IS NULL").fetchall()]
    except sqlite3.OperationalError:
        return out
    if not missing:
        return out
    db_key = str(kbc._main_db_file(conn) or id(conn))
    own = own_gateway(conn)
    foreign = foreign_ids(conn, own)
    marks = ",".join("?" for _ in kc.SCHEDULED_ENTRY_EVENTS)
    for tid in missing:
        park = conn.execute(
            f"SELECT id, payload, created_at FROM task_events WHERE task_id = ? AND kind IN ({marks}) "
            "ORDER BY id DESC LIMIT 1", (tid, *kc.SCHEDULED_ENTRY_EVENTS)).fetchone()
        if park is None:
            continue
        park_id, payload, parked_at = int(park[0]), kb._json_dict(park[1]), int(park[2] or 0)
        key = (db_key, tid)
        if _RESTORE_SEEN.get(key) == park_id:
            continue
        cmd = payload.get("wake_check")
        if not cmd or not isinstance(payload.get("wake_at"), (int, float)):
            _RESTORE_SEEN[key] = park_id  # a clock-only park: nothing to restore until it parks again
            continue
        if conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND id > ? AND kind IN ('claimed', 'spawned') "
                        "LIMIT 1", (tid, park_id)).fetchone():
            continue  # a later run's park has not reached this board yet
        every = normalize_every(payload.get("check_every"))
        host = payload.get("probe_host")
        copy = tid in foreign
        base = {"wake_check": cmd, "check_every": every, "probe_host": host}
        if host and own and host != own:
            if copy:
                continue  # another gateway's probe on a card another gateway owns
            last = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND id > ? AND kind = ?",
                                (tid, park_id, ALIVE_EVENT)).fetchone()[0]
            silent = now - max(parked_at, int(last or 0))
            if silent <= ALIVE_EVERY + 2 * every + SILENT_GRACE:
                continue
            with kb.write_txn(conn):
                if _lost_wake(conn, tid, {**base, "reason": "remote_probe_silent", "silent_seconds": silent},
                              f"Woken: this card's wake_check runs on {host}, the gateway that parked it, and "
                              f"{host} has not reported that probe for {silent // 60} min, so nothing is watching "
                              f"the condition. Probe: `{cmd}`. Check the condition yourself, then park again."):
                    out["woken"].append(tid)
            continue
        if copy and not host:
            continue  # a legacy park on a worker node's copy: its home board restores it
        with kb.write_txn(conn):
            st = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
            if st is None or st[0] != "scheduled" or get_state(conn, tid) is not None:
                continue
            restored = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND id > ? AND kind = ?",
                                    (tid, park_id, RESTORED_EVENT)).fetchone()[0]
            if not restored:
                set_check(conn, tid, cmd, every, now=now)
                kb._append_event(conn, tid, RESTORED_EVENT, {**base, "park_event_id": park_id})
                out["restored"].append(tid)
            elif _lost_wake(conn, tid, {**base, "reason": "lost_again"},
                            f"Woken: this card's wake_check probe went missing twice while it was parked, so "
                            f"nothing was watching the condition. Probe: `{cmd}`. Check the condition yourself, "
                            "then park again."):
                out["woken"].append(tid)
    return out


# --- state table --------------------------------------------------------------------------

def set_check(conn: sqlite3.Connection, task_id: str, cmd: Optional[str], every: Optional[int],
              *, now: Optional[int] = None, register_here: bool = True) -> None:
    """Record the logical probe, registering execution only on its designated host.

    Caller holds the write txn. Clearing a relayed execution row is not a change
    to the logical probe or its episode. The first local probe is due at once.
    """
    episode.repark(conn, task_id, cmd)
    conn.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (task_id,))
    if not cmd or not register_here:
        return
    now = int(time.time()) if now is None else int(now)
    conn.execute(
        "INSERT INTO task_wake_checks (task_id, cmd, every, next_at, fails, generation) "
        "VALUES (?, ?, ?, ?, 0, ?)",
        (task_id, cmd, normalize_every(every), now, uuid.uuid4().hex))


def _row_dict(row) -> dict:
    keys = ("task_id", "cmd", "every", "next_at", "last_at", "last_rc", "last_out", "fails", "generation", "purpose",
            "episode_first_at", "episode_checks", "episode_last_stale_at")
    return dict(zip(keys, row))


_LEGACY_SELECT = "SELECT task_id, cmd, every, next_at, last_at, last_rc, last_out, fails, generation, purpose"
_SELECT = (_LEGACY_SELECT + ", "
           "(SELECT first_at FROM task_wake_episodes e WHERE e.task_id = task_wake_checks.task_id), "
           "(SELECT checks FROM task_wake_episodes e WHERE e.task_id = task_wake_checks.task_id), "
           "(SELECT last_stale_at FROM task_wake_episodes e WHERE e.task_id = task_wake_checks.task_id) "
           "FROM task_wake_checks")
# [daveey] Which status each probe purpose runs in: a park's wake_check while ``scheduled``, a human ask's
# resolved_check (``kanban_ask_resolved``) while ``blocked``. A row whose card left that status is stale.
PURPOSE_STATUS = {"wake": "scheduled", "ask_resolved": "blocked"}
_LIVE_ROW = ("((w.purpose = 'ask_resolved' AND t.status = 'blocked') "
             "OR (w.purpose IS NOT 'ask_resolved' AND t.status = 'scheduled'))")


def get_state(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    try:
        row = _state_rows(conn, " WHERE task_id = ?", (task_id,)).fetchone()
    except sqlite3.OperationalError:  # board created by older code, read-only fenced conn
        return None
    return _row_dict(row) if row else None


def load_states(conn: sqlite3.Connection, task_ids: Optional[list[str]] = None) -> dict[str, dict]:
    try:
        if task_ids is None:
            rows = _state_rows(conn).fetchall()
        elif not task_ids:
            return {}
        else:
            marks = ",".join("?" for _ in task_ids)
            rows = _state_rows(conn, f" WHERE task_id IN ({marks})", tuple(task_ids)).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r[0]: _row_dict(r) for r in rows}


def _state_rows(conn: sqlite3.Connection, suffix: str = "", params: tuple = ()):
    try:
        return conn.execute(_SELECT + suffix, params)
    except sqlite3.OperationalError as exc:
        if "no such table: task_wake_episodes" not in str(exc):
            raise
        # A fenced read-only board may still be owned by a pre-upgrade process.
        return conn.execute(_LEGACY_SELECT + ", NULL, NULL, NULL FROM task_wake_checks" + suffix, params)


def describe_state(state: dict, *, now: Optional[int] = None) -> str:
    """One line for ``kanban show`` / worker context."""
    now = int(time.time()) if now is None else int(now)

    def stamp(ts):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"

    last = ("not run yet" if state.get("last_at") is None
            else f"last run {stamp(state['last_at'])} (last rc {state.get('last_rc')})")
    fails = f", {state['fails']} failing in a row" if state.get("fails") else ""
    due = "due now" if state["next_at"] <= now else f"next {stamp(state['next_at'])}"
    what = "ask resolved_check " if state.get("purpose") == "ask_resolved" else ""
    pending = (f"; not passed since {episode.stamp(state['episode_first_at'])} "
               f"({state['episode_checks']} checks)" if state.get("episode_first_at") is not None else "")
    return f"{what}`{state['cmd']}` every {state['every']}s; {last}{fails}; {due}{pending}"


# --- probe runner -------------------------------------------------------------------------

_LOCK = threading.Lock()
_POOL: Optional[ThreadPoolExecutor] = None
_IN_FLIGHT: dict[tuple[str, str], Future] = {}
_PROCS: dict[tuple[str, str], subprocess.Popen] = {}


def _pool() -> ThreadPoolExecutor:
    global _POOL
    with _LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="kanban-wake-check")
        return _POOL


def in_flight() -> list[str]:
    with _LOCK:
        return sorted(tid for (_, tid), fut in _IN_FLIGHT.items() if not fut.done())


def _shutdown_for_tests() -> None:
    """Kill every running probe and drop the pool (tests only)."""
    global _POOL
    with _LOCK:
        procs = list(_PROCS.values())
        pool, _POOL = _POOL, None
    for proc in procs:
        _kill(proc)
    if pool is not None:
        pool.shutdown(wait=True, cancel_futures=True)
    with _LOCK:
        _IN_FLIGHT.clear()
        _PROCS.clear()


def _kill(proc: subprocess.Popen) -> None:
    from agent.deadline import kill_process_tree

    if not kill_process_tree(proc.pid):
        try:
            proc.kill()
        except OSError:
            pass


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text[-OUTPUT_TAIL_CHARS:]


def _profile_home(assignee: Optional[str], task_id: Optional[str] = None) -> Optional[str]:
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    try:
        profile = normalize_profile_name(assignee) if assignee else None
        return str(resolve_profile_env(profile)) if profile else None
    except (FileNotFoundError, ValueError, OSError):
        logger.debug("wake check: no profile home for %s", task_id, exc_info=True)
        return None


def _probe_env(task, db_path: str) -> dict:
    """The assignee profile's env (``served_profile_child_env``: its HERMES_HOME and its own
    secrets, never the launch profile's) plus the board pin, so ``hermes kanban show`` in a
    probe reads the card's board."""
    return probe_env(task.id, task.assignee, db_path)


def probe_env(task_id: Optional[str], assignee: Optional[str], db_path: str) -> dict:
    """``_probe_env`` by parts. ``task_id=None`` [daveey] is a card not created yet (an ask filed at create,
    ``kanban_ask_resolved.create_context``): the env has no ``HERMES_KANBAN_TASK`` at all, never the
    creating worker's own card id."""
    from agent.secret_scope import UnscopedSecretError
    from tools.environments.local import served_profile_child_env

    home = _profile_home(assignee, task_id)
    try:
        env = served_profile_child_env(target_home=home, inherit_credentials=True)
    except UnscopedSecretError:  # multiplex with no profile home: no credentials, not the launcher's
        logger.debug("wake check: no secret scope for %s", task_id, exc_info=True)
        env = served_profile_child_env(target_home=home, inherit_credentials=False)
    for key in ("HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_SESSION_ID", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_TASK"):
        env.pop(key, None)
    if task_id:
        env["HERMES_KANBAN_TASK"] = task_id
    env["HERMES_KANBAN_DB"] = db_path
    env["HERMES_KANBAN_WAKE_PROBE"] = "1"  # wait-check's implicit --from applies only to probe runs [daveey]
    return env


def probe_cwd(workspace_path: Optional[str]) -> str:
    """The cwd a card's probe runs in: its workspace when that is an existing absolute dir, else home."""
    ws = workspace_path
    return ws if ws and os.path.isabs(ws) and os.path.isdir(ws) else str(Path.home())


def _run_probe(key: tuple[str, str], cmd: str, cwd: str, env: dict) -> tuple[Any, str]:
    """``(rc, output)``; rc is ``"timeout"`` when the probe outlived ``PROBE_TIMEOUT_SECONDS``."""
    try:
        proc = subprocess.Popen(
            ["bash", "-lc", cmd], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            # detached-spawn: ok — awaited with PROBE_TIMEOUT_SECONDS; _kill ends the whole group on timeout [daveey]
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        return 127, f"could not start probe: {exc}"
    with _LOCK:
        _PROCS[key] = proc
    try:
        out, _ = proc.communicate(timeout=PROBE_TIMEOUT_SECONDS)
        return proc.returncode, out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        _kill(proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            out = b""
        return "timeout", (out or b"").decode("utf-8", "replace") + f"\n[timed out after {PROBE_TIMEOUT_SECONDS}s]"
    finally:
        with _LOCK:
            _PROCS.pop(key, None)


def _probe_diagnostics(cwd: str, env: dict) -> dict:
    """Where a probe ran, so a false wake can be diagnosed from its ``condition_met`` event alone."""
    import socket

    return {"pid": os.getpid(), "host": socket.gethostname(), "cwd": cwd, "home": env.get("HOME"),
            "path_head": [p for p in (env.get("PATH") or "").split(os.pathsep)[:3]]}


# [daveey] A park's own probe can be blind to its producer: MacBook t_01a17ec75ca4f9f6 probed a hub card with
# `hermes kanban show` (this host's board only) and read a clean "not yet" for 18 h after the card was done. On
# every "not yet" the runner re-checks the producer card the park's ``awaiting`` names. A remote check is ssh, so
# it runs at most once per PRODUCER_RECHECK_SECONDS per (gateway, ref, board), across ticks and parks.
PRODUCER_RECHECK_SECONDS = 15 * 60
PRODUCER_FINISHED = "producer_finished"
_FINISHED_STATUSES = ("done", "archived")
_PRODUCER_CHECKED: dict[tuple[str, str, Optional[str]], tuple[float, int, str]] = {}


def _remote_producer(gateway: str, ref: str, board: Optional[str]) -> tuple[int, str]:
    """``check_remote`` for a producer card, rate-limited. Never raises (rc 2 = cannot tell)."""
    from hermes_cli import kanban_wait_check as kwait

    key = (gateway, ref, board)
    with _LOCK:
        seen = _PRODUCER_CHECKED.get(key)
    if seen and time.monotonic() - seen[0] < PRODUCER_RECHECK_SECONDS:
        return seen[1], seen[2]
    try:
        rc, out = kwait.check_remote(gateway, "task", ref, board=board)
    except Exception as exc:  # cannot tell: never a wake
        logger.debug("producer re-check of %s on %s failed", ref, gateway, exc_info=True)
        rc, out = 2, f"error: {exc}"
    with _LOCK:
        _PRODUCER_CHECKED[key] = (time.monotonic(), rc, out)
    return rc, out


def _remote_completed_at(gateway: str, ref: str, board: Optional[str]) -> Optional[int]:
    """The remote card's ``completed_at`` from this host's hermes-hub replica, when it has one. Best effort."""
    try:
        from hermes_cli.kanban_wait_replica import StaleReplica, UnknownScope, read_task
        try:
            task = read_task(gateway, ref, board=board)
        except (StaleReplica, UnknownScope) as exc:
            task = exc.task
        value = task.get("completed_at")
        return int(value) if value else None
    except Exception:
        logger.debug("replica completed_at for %s on %s unreadable", ref, gateway, exc_info=True)
        return None


def finished_producer(conn: sqlite3.Connection, task_id: str, cmd: str) -> Optional[dict]:
    """The producer card this park's ``awaiting`` names, when it is done or archived although the park's probe
    still exits 1; else None. A remote ref is read over ssh: call this outside any write txn. Each ref wakes a
    card once (a ref already reported by a ``wake_probe_stale`` event on this card is skipped)."""
    from hermes_cli.kanban_awaiting import _TASK_ID, _latest_park
    from hermes_cli.kanban_comment_origin import WAKE_PROBE_STALE_EVENT

    try:
        awaiting, _reason = _latest_park(conn, task_id)
        if not isinstance(awaiting, dict) or awaiting.get("kind") != "card":
            return None
        ref = str(awaiting.get("ref") or "")
        if not _TASK_ID.fullmatch(ref) or ref == task_id:
            return None
        gateway = awaiting.get("gateway") or None
        board = awaiting.get("board") or None
        if gateway and "wait-check" in (cmd or "") and ref in (cmd or ""):
            return None  # the probe already reads that card over the same path
        for (payload,) in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                                       (task_id, WAKE_PROBE_STALE_EVENT)):
            try:
                if json.loads(payload or "{}").get("ref") == ref:
                    return None
            except (ValueError, AttributeError):
                continue
        if gateway:
            rc, out = _remote_producer(str(gateway), ref, board)
            if rc not in (0, 1):  # 2 cannot tell, 3 gateway offline (stale replica): unknown, never a wake
                return None
            words = out.split()
            status = words[1] if len(words) >= 2 and words[0] == ref else ""
            completed = _remote_completed_at(str(gateway), ref, board) if status in _FINISHED_STATUSES else None
        else:
            from hermes_cli.kanban_awaiting_board import producer_connection
            with producer_connection(conn, awaiting) as owner:
                row = owner.execute("SELECT status, completed_at FROM tasks WHERE id = ?", (ref,)).fetchone()
            if row is None:
                return None
            status, completed = row[0], row[1]
        if status not in _FINISHED_STATUSES:
            return None  # review/blocked are a tracker's handled states, as in confirm_remote: never a wake
        when = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(completed))) if completed
                else "at an unrecorded time")
        return {"ref": ref, "gateway": gateway, "board": board, "producer_status": status, "completed_at": when}
    except Exception:
        logger.debug("producer re-check for %s failed", task_id, exc_info=True)
        return None


def producer_note(hit: dict) -> str:
    ref, gw = hit["ref"], hit.get("gateway")
    board = hit.get("board")
    where = gw or board or "this board"
    scope = f" --gateway {gw}" if gw else ""
    scope += f" --board {board}" if board else ""
    return (f"Woken: producer card {ref} on {where} is {hit['producer_status']} (completed {hit['completed_at']}), but this park's "
            f"wake_check still exits 1, so it can't see that card (`hermes kanban show` reads only this host's "
            f"board). Read {ref}'s result and act on it. To wait on the producer's board, use "
            f"`hermes kanban wait-check task {ref}{scope}`, or omit wake_check: a card wait gets "
            "that probe automatically. If that card is already blocked, in review or in triage, add "
            "`--from <that status>`. Those statuses count as met, so without it the probe passes at once.")


def _probe_and_apply(db_path: str, task_id: str, cmd: str, cwd: str, env: dict, now: Optional[int], generation: str,
                     purpose: str = "wake", stale_interval: Optional[float] = None) -> Any:
    key = (db_path, task_id)
    try:
        rc, out = _run_probe(key, cmd, cwd, env)
        diag = None
        if rc == 0 and purpose != "ask_resolved":  # an ask's exit 0 is read literally (kanban_ask_resolved)
            # One stray exit 0 must not wake a card (a stolen wait status once read as 0): confirm
            # with a second run and wake only if it passes again. The second result is what counts.
            first = {"first_rc": rc, "first_output": _tail(out)}
            rc, out = _run_probe(key, cmd, cwd, env)
            diag = {**first, "confirm_rc": rc, **_probe_diagnostics(cwd, env)}
        from hermes_cli import kanban_db_connect as kbc
        conn = kbc.connect(Path(db_path))
        try:
            if purpose == "ask_resolved":
                from hermes_cli import kanban_ask_resolved as kar
                kar.apply_result(conn, task_id, cmd, rc, out, now=now, generation=generation)
                return rc
            answered = None
            if rc == 1 and not output_error(out):
                # [daveey] The probe says "not yet" but the hermes-hub request this park names is answered:
                # the probe cannot see the reply (t_01a17f81686694ec, 38 h). wake_probe_stale wakes it once.
                from hermes_cli.kanban_comment_origin import answered_request
                answered = answered_request(conn, task_id, cmd)
            producer = None
            if rc == 1 and not output_error(out) and not answered:
                # [daveey] Same failure, another producer: the card this park awaits is done but the probe can't
                # see it (a `hermes kanban show` of another gateway's card). No write txn is open here (ssh).
                producer = finished_producer(conn, task_id, cmd)
            dead_ancestry = None
            if rc == 1 and not output_error(out) and not answered and not producer:
                from hermes_cli.kanban_dead_ancestry import detect
                dead_ancestry = detect(cmd, cwd)
            apply_result(conn, task_id, cmd, rc, out, now=now, generation=generation, diagnostics=diag,
                         answered=answered, producer=producer, dead_ancestry=dead_ancestry,
                         stale_interval=stale_interval)
        finally:
            conn.close()
        return rc
    except Exception:
        logger.warning("kanban wake check for %s failed to run/apply", task_id, exc_info=True)
        return None


def apply_result(conn: sqlite3.Connection, task_id: str, cmd: str, rc: Any, out: str,
                 *, generation: str, now: Optional[int] = None,
                 diagnostics: Optional[dict] = None, answered: Optional[dict] = None,
                 producer: Optional[dict] = None, dead_ancestry: Optional[list[dict]] = None,
                 stale_interval: Optional[float] = None) -> Optional[str]:
    """Record one probe result; wake the card on success or a broken probe. Returns the event
    kind appended (``condition_met`` / ``wake_check_broken``) or None. A result for a probe
    that is no longer the card's (re-parked, unblocked) is dropped."""
    from hermes_cli import kanban_db as kb

    now = int(time.time()) if now is None else int(now)
    tail = _tail(out)
    interval = episode.stale_seconds() if stale_interval is None else stale_interval
    with kb.write_txn(conn):
        state = get_state(conn, task_id)
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if (state is None or state["cmd"] != cmd or state["generation"] != generation
                or state.get("purpose") == "ask_resolved" or status is None or status[0] != "scheduled"):
            return None
        rc_col = rc if isinstance(rc, int) else -1
        episode.reconcile(conn, task_id)
        if rc == 0 or (diagnostics and diagnostics.get("first_rc") == 0):
            episode.reset(conn, task_id)
            if rc != 0:
                kb._append_event(conn, task_id, "wake_episode_reset")
        if rc == 0:
            return _wake(conn, task_id, CONDITION_MET_EVENT, {
                "wake_check": cmd, "output": tail, **({"diagnostics": diagnostics} if diagnostics else {})})
        if rc == 1 and not output_error(out) and answered:
            from hermes_cli.kanban_comment_origin import WAKE_PROBE_STALE_EVENT, stale_note
            note = stale_note(answered, cmd)
            woke = _wake(conn, task_id, WAKE_PROBE_STALE_EVENT, {
                "wake_check": cmd, "output": tail, "request": answered["request"], "owner": answered["owner"],
                "note": note})
            if woke:
                kb.add_comment(conn, task_id, "kanban-dispatcher", note)
            return woke
        if rc == 1 and not output_error(out) and producer:
            from hermes_cli.kanban_comment_origin import WAKE_PROBE_STALE_EVENT
            note = producer_note(producer)
            woke = _wake(conn, task_id, WAKE_PROBE_STALE_EVENT, {
                "reason": PRODUCER_FINISHED, "wake_check": cmd, "output": tail, "ref": producer["ref"],
                "gateway": producer.get("gateway"), "board": producer.get("board"),
                "producer_status": producer["producer_status"], "completed_at": producer["completed_at"],
                "note": note})
            if woke:
                kb.add_comment(conn, task_id, "kanban-dispatcher", note)
            return woke
        if rc == 1 and not output_error(out):
            if dead_ancestry:
                from hermes_cli.kanban_dead_ancestry import wake_once
                woke = wake_once(conn, task_id, cmd, dead_ancestry)
                if woke:
                    return woke
            stale = episode.not_yet(conn, task_id, cmd, now, interval)
            if stale:
                woke = _wake(conn, task_id, episode.STALE_EVENT, stale)
                if woke:
                    kb.add_comment(conn, task_id, "kanban-dispatcher", stale["note"])
                return woke
            conn.execute(
                "UPDATE task_wake_checks SET last_at = ?, last_rc = 1, last_out = ?, fails = 0, "
                "next_at = ? WHERE task_id = ?", (now, tail, now + int(state["every"]), task_id))
            _note_alive(conn, task_id, cmd, now)
            return None
        age = stale_age(rc, out)
        if age is not None:
            from hermes_cli.kanban_offline_backoff import recently_escalated
            m = STALE_GATEWAY_RE.search(out or "")
            gw = m.group(1).rstrip(",;)") if m else "the producer's gateway"
            if age >= STALE_WAKE_AFTER and not recently_escalated(conn, task_id, cmd, gw, now, STALE_WAKE_AFTER,
                                                                  reasons=SOURCE_UNAVAILABLE_REASONS):
                hours = age / 3600
                return _wake(conn, task_id, BROKEN_EVENT, {
                    "wake_check": cmd, "rc": rc, "fails": 0, "output": tail, "reason": "gateway_offline",
                    "gateway": gw, "offline_seconds": age,
                    "note": (f"Gateway {gw} offline for {hours:.0f} h: its replica has not synced, so the producer "
                             "card's status is unknown. Check whether that machine is up, then re-check and park "
                             "again (or replan without it).")})
            # Not broken: the gateway is just offline. Keep any real-failure streak from before as it was.
            conn.execute(
                "UPDATE task_wake_checks SET last_at = ?, last_rc = ?, last_out = ?, "
                "next_at = ? WHERE task_id = ?", (now, rc, tail, now + int(state["every"]), task_id))
            _note_alive(conn, task_id, cmd, now)
            return None
        gw = source_unknown(rc, out)
        if gw is not None:
            # The owner is publishing but says it could not read this board: unknown, not yet, never "broken".
            # Escalate once; while that (or an offline) escalation is under 24 h old the same probe stays parked.
            from hermes_cli.kanban_offline_backoff import recently_escalated
            if not recently_escalated(conn, task_id, cmd, gw, now, STALE_WAKE_AFTER,
                                      reasons=SOURCE_UNAVAILABLE_REASONS):
                return _wake(conn, task_id, BROKEN_EVENT, {
                    "wake_check": cmd, "rc": rc, "fails": 0, "output": tail, "reason": SCOPE_UNKNOWN_REASON,
                    "gateway": gw,
                    "note": (f"Gateway {gw} is syncing but reports it could not read its board, so the producer "
                             "card's status is unknown (its last-seen status is kept, not trusted). Check that "
                             "gateway's Hermes runtime and hermes-hub collector, then re-check and park again on "
                             "the same probe (identical unknown results are then held for 24 h), or replan.")})
            # Same unchanged source state, already escalated: keep any real-failure streak as it was.
            conn.execute(
                "UPDATE task_wake_checks SET last_at = ?, last_rc = ?, last_out = ?, "
                "next_at = ? WHERE task_id = ?", (now, rc, tail, now + int(state["every"]), task_id))
            _note_alive(conn, task_id, cmd, now)
            return None
        fails = int(state["fails"] or 0) + 1
        if rc == 1:  # exit 1 with error output: a crashed probe posing as "not yet"
            rc, rc_col = f"exit 1 with error output: {output_error(out)}", 1
        if fails >= BROKEN_AFTER:
            return _wake(conn, task_id, BROKEN_EVENT, {
                "wake_check": cmd, "rc": rc, "fails": fails, "output": tail,
                "note": "The wake_check failed (not exit 0/1) repeatedly. Fix the probe, then park again."})
        conn.execute(
            "UPDATE task_wake_checks SET last_at = ?, last_rc = ?, last_out = ?, fails = ?, "
            "next_at = ? WHERE task_id = ?", (now, rc_col, tail, fails, now + int(state["every"]), task_id))
        _note_alive(conn, task_id, cmd, now)
        return None


def _wake(conn: sqlite3.Connection, task_id: str, kind: str, payload: dict) -> Optional[str]:
    """``scheduled`` -> its resumable phase, as ``wake_due_continuations`` does. In a txn."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_continuation import pending_wake

    park = pending_wake(conn, task_id) or {}
    resume = park.get("resume_status") if park.get("resume_status") in ("ready", "review") else "ready"
    landing = kb._landing_status_after_parents(conn, task_id)
    new_status = "review" if landing == "ready" and resume == "review" else landing
    cur = conn.execute(
        "UPDATE tasks SET status = ?, current_run_id = NULL WHERE id = ? AND status = 'scheduled'",
        (new_status, task_id))
    if cur.rowcount != 1:
        return None
    conn.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (task_id,))
    kb._append_event(conn, task_id, kind, {**payload, "status": new_status, "wake_at": park.get("wake_at")})
    return kind


def run_due_wake_checks(conn: sqlite3.Connection, *, now: Optional[int] = None, wait: bool = False) -> dict:
    """Start every due probe (at most ``MAX_CONCURRENT`` in flight) and return at once;
    ``wait=True`` (tests, one-shot callers) also waits up to ``TICK_BUDGET_SECONDS`` for them.
    Returns ``{"submitted": [task ids], "in_flight": n}``. Never raises."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    submitted: list[str] = []
    futures: list[Future] = []
    try:
        db_path = kbc._main_db_file(conn)
        if not db_path:
            return {"submitted": [], "in_flight": 0}
        ts = int(time.time()) if now is None else int(now)
        stale_interval = episode.stale_seconds()  # resolve before crossing into unscoped pool threads
        try:
            stale = conn.execute(
                "SELECT w.task_id FROM task_wake_checks w LEFT JOIN tasks t ON t.id = w.task_id "
                f"WHERE t.id IS NULL OR NOT {_LIVE_ROW}").fetchall()
        except sqlite3.OperationalError:
            return {"submitted": [], "in_flight": 0}
        if stale:
            with kb.write_txn(conn):
                for row in stale:
                    conn.execute("DELETE FROM task_wake_checks WHERE task_id = ? AND task_id IN (SELECT w.task_id "
                                 "FROM task_wake_checks w LEFT JOIN tasks t ON t.id = w.task_id "
                                 f"WHERE t.id IS NULL OR NOT {_LIVE_ROW})", (row[0],))
        restore_missing_checks(conn, now=ts)  # [daveey] a park whose probe never reached this board
        from hermes_cli import kanban_ask_resolved as kar
        kar.reconcile(conn, now=ts)  # [daveey] an ask's probe row follows its stored ask; a lost one is restored
        due = conn.execute(
            "SELECT w.task_id, w.cmd, w.every, w.generation, w.purpose FROM task_wake_checks w "
            f"JOIN tasks t ON t.id = w.task_id WHERE {_LIVE_ROW} AND w.next_at <= ? ORDER BY w.next_at",
            (ts,)).fetchall()
        for task_id, cmd, every, generation, purpose in due:
            key = (db_path, task_id)
            with _LOCK:
                busy = sum(1 for f in _IN_FLIGHT.values() if not f.done())
                running = key in _IN_FLIGHT and not _IN_FLIGHT[key].done()
            if running:
                continue
            if busy >= MAX_CONCURRENT:
                break
            task = kb.get_task(conn, task_id)
            if task is None:
                continue
            # Claim the slot first so a crash mid-probe cannot re-run it every tick.
            claim = uuid.uuid4().hex
            with kb.write_txn(conn):
                claimed = conn.execute(
                    "UPDATE task_wake_checks SET next_at = ?, generation = ? "
                    "WHERE task_id = ? AND cmd = ? AND generation IS ? AND next_at <= ? AND purpose IS ? "
                    "AND EXISTS (SELECT 1 FROM tasks WHERE id = ? AND status = ?)",
                    (ts + int(every), claim, task_id, cmd, generation, ts, purpose, task_id,
                     PURPOSE_STATUS.get(purpose, "scheduled")))
                if claimed.rowcount != 1:
                    continue
            cwd = probe_cwd(task.workspace_path)
            fut = _pool().submit(_probe_and_apply, db_path, task_id, cmd, cwd, _probe_env(task, db_path), now, claim,
                                 purpose, stale_interval)
            with _LOCK:
                _IN_FLIGHT[key] = fut
            submitted.append(task_id)
            futures.append(fut)
        with _LOCK:
            for k in [k for k, f in _IN_FLIGHT.items() if f.done()]:
                _IN_FLIGHT.pop(k, None)
    except Exception:
        logger.warning("kanban wake checks: tick failed", exc_info=True)
    if wait and futures:
        deadline = time.monotonic() + TICK_BUDGET_SECONDS
        for fut in futures:
            try:
                fut.result(timeout=max(0.0, deadline - time.monotonic()))
            except (FutureTimeout, CancelledError):
                logger.debug("kanban wake check still running after the wait budget")
    return {"submitted": submitted, "in_flight": len(in_flight())}


# --- worker context -----------------------------------------------------------------------

def render_wake_context(conn: sqlite3.Connection, task_id: str) -> str:
    """Why this run woke (``condition_met`` / ``wake_check_broken`` after the last park), or
    the live probe while the card is parked. Advisory: never raises (empty on failure)."""
    try:
        return _render_wake_context(conn, task_id)
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        logger.debug("wake check context failed for %s", task_id, exc_info=True)
        return ""


def _render_wake_context(conn: sqlite3.Connection, task_id: str) -> str:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_continuation import SCHEDULED_ENTRY_EVENTS

    lines: list[str] = []
    from hermes_cli.kanban_comment_origin import WAKE_PROBE_STALE_EVENT

    kinds = (*SCHEDULED_ENTRY_EVENTS, CONDITION_MET_EVENT, BROKEN_EVENT, WAKE_PROBE_STALE_EVENT, episode.STALE_EVENT)
    marks = ",".join("?" for _ in kinds)
    row = conn.execute(
        f"SELECT kind, payload FROM task_events WHERE task_id = ? AND kind IN ({marks}) "
        "ORDER BY id DESC LIMIT 1", (task_id, *kinds)).fetchone()
    if row is not None and row[0] in (CONDITION_MET_EVENT, BROKEN_EVENT, WAKE_PROBE_STALE_EVENT, episode.STALE_EVENT):
        payload = kb._json_dict(row[1])
        if row[0] == episode.STALE_EVENT:
            return f"## Woken to reconsider a never-passing wake check\n{payload.get('note', '')}\n"
        if row[0] == WAKE_PROBE_STALE_EVENT and payload.get("reason") == PRODUCER_FINISHED:
            lines.append("## Woken because the awaited producer card finished but the wake check missed it")
            lines.append(f"{payload.get('note')} Last output:")
        elif row[0] == WAKE_PROBE_STALE_EVENT:
            lines.append("## Woken because the awaited reply arrived but the wake check missed it")
            lines.append(f"{payload.get('note')} Last output:")
        elif row[0] == CONDITION_MET_EVENT:
            lines.append("## Woken by wake check (condition met)")
            lines.append(f"The park's wake_check `{payload.get('wake_check')}` exited 0, so this card "
                         "woke before its deadline. Its output:")
        elif payload.get("reason") == "gateway_offline":
            lines.append("## Woken because the producer's gateway is offline")
            lines.append(f"{payload.get('note')} Probe: `{payload.get('wake_check')}`. Last output:")
        elif payload.get("reason") == SCOPE_UNKNOWN_REASON:
            lines.append("## Woken because the producer's gateway could not read its board")
            lines.append(f"{payload.get('note')} Probe: `{payload.get('wake_check')}`. Last output:")
        else:
            lines.append("## Woken because the wake check is broken")
            lines.append(f"The park's wake_check `{payload.get('wake_check')}` failed "
                         f"{payload.get('fails')} times in a row (last rc {payload.get('rc')}). Fix the "
                         "probe (exit 0 = condition holds, 1 = not yet), then re-check the condition "
                         "yourself and park again if it still does not hold. Last output:")
        lines += ["```", payload.get("output") or "(no output)", "```", ""]
        return "\n".join(lines) + "\n"
    state = get_state(conn, task_id)
    if state is not None:
        lines += ["## Wake check", describe_state(state), ""]
        return "\n".join(lines) + "\n"
    return ""
