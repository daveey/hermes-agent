"""Condition wakes: a ``scheduled`` card wakes when a cheap shell probe passes.

A timed park (``kanban_schedule``, ``kanban_block`` kind=time, ``kanban_continue`` with a
wait) may carry ``wake_check`` (a shell command) and ``check_every`` (seconds). The
dispatcher probes it off-thread on each tick:

* exit 0 -> the condition holds: the card wakes now (parents re-gated, ``resume_status``
  honoured, like ``wake_due_continuations``) with a ``condition_met`` event carrying the
  stdout tail, which the next worker's context shows;
* exit 1 -> not yet: probe again ``check_every`` seconds later;
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

import logging
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_CHECK_EVERY = 900
MIN_CHECK_EVERY = 120
MAX_CHECK_EVERY = 21600
PROBE_TIMEOUT_SECONDS = 30
MAX_CONCURRENT = 4
# ``run_due_wake_checks(wait=True)`` never waits longer than this; the dispatcher never waits.
TICK_BUDGET_SECONDS = 60
BROKEN_AFTER = 3
GUARD_SECONDS = 2 * 3600
OUTPUT_TAIL_CHARS = 500
MAX_CMD_CHARS = 2000
CONDITION_MET_EVENT = "condition_met"
BROKEN_EVENT = "wake_check_broken"


GUARD_HELP = (
    "A wait on any readable fact needs wake_check=<shell command>: exit 0 = the condition "
    "holds (the card wakes at once), exit 1 = not yet (re-probed every check_every seconds, "
    "default 900, min 120). Examples: "
    "`sqlite3 ~/.bb/hub.db \"select build from devices where name='watch'\" | grep -q 559`, "
    "`gh api repos/OWNER/REPO/tags --jq '.[].name' | grep -q '^desktop-client-v'`, "
    "`hermes kanban show t_xxxx --json | jq -e '.task.status==\"done\"'`. "
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
                 resume_after: int = 0, now: Optional[int] = None) -> Optional[int]:
    """The wake time a park call would set, or None when the call is not a timed park
    (``op`` is ``schedule``, ``block`` or ``continue``)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_timed_park as ktp

    now = int(time.time()) if now is None else int(now)
    if op == "schedule":
        return ktp.scheduled_payload(reason, now=now)["wake_at"]
    if op == "block":
        if blocker_id or not ktp.is_timed_park(
                kind, reason, parents_satisfied=kb._parents_satisfied(conn, task_id)):
            return None
        return ktp.scheduled_until(reason, now=now) or now + ktp.DEFAULT_WAKE_SECONDS
    if op == "continue":
        return now + int(resume_after) if resume_after and int(resume_after) > 0 else None
    raise ValueError(f"unknown park op {op!r}")


def park_fields(wake_check: Optional[str], check_every: Optional[int], clock_only: bool) -> dict:
    """Keys added to the parking event payload (only the ones that are set)."""
    out: dict = {}
    if wake_check:
        out["wake_check"] = wake_check
        out["check_every"] = normalize_every(check_every)
    if clock_only:
        out["clock_only"] = True
    return out


# --- state table --------------------------------------------------------------------------

def set_check(conn: sqlite3.Connection, task_id: str, cmd: Optional[str], every: Optional[int],
              *, now: Optional[int] = None) -> None:
    """Record (or clear, when ``cmd`` is None) the card's probe. Caller holds the write txn.
    The first probe is due at once: the condition may have come true while the worker parked."""
    conn.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (task_id,))
    if not cmd:
        return
    now = int(time.time()) if now is None else int(now)
    conn.execute(
        "INSERT INTO task_wake_checks (task_id, cmd, every, next_at, fails, generation) "
        "VALUES (?, ?, ?, ?, 0, ?)",
        (task_id, cmd, normalize_every(every), now, uuid.uuid4().hex))


def _row_dict(row) -> dict:
    keys = ("task_id", "cmd", "every", "next_at", "last_at", "last_rc", "last_out", "fails", "generation")
    return {k: row[i] for i, k in enumerate(keys)}


_SELECT = "SELECT task_id, cmd, every, next_at, last_at, last_rc, last_out, fails, generation FROM task_wake_checks"


def get_state(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    try:
        row = conn.execute(_SELECT + " WHERE task_id = ?", (task_id,)).fetchone()
    except sqlite3.OperationalError:  # board created by older code, read-only fenced conn
        return None
    return _row_dict(row) if row else None


def load_states(conn: sqlite3.Connection, task_ids: Optional[list[str]] = None) -> dict[str, dict]:
    try:
        if task_ids is None:
            rows = conn.execute(_SELECT).fetchall()
        elif not task_ids:
            return {}
        else:
            marks = ",".join("?" for _ in task_ids)
            rows = conn.execute(_SELECT + f" WHERE task_id IN ({marks})", tuple(task_ids)).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r[0]: _row_dict(r) for r in rows}


def describe_state(state: dict, *, now: Optional[int] = None) -> str:
    """One line for ``kanban show`` / worker context."""
    now = int(time.time()) if now is None else int(now)

    def stamp(ts):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"

    last = ("not run yet" if state.get("last_at") is None
            else f"last run {stamp(state['last_at'])} (last rc {state.get('last_rc')})")
    fails = f", {state['fails']} failing in a row" if state.get("fails") else ""
    due = "due now" if state["next_at"] <= now else f"next {stamp(state['next_at'])}"
    return f"`{state['cmd']}` every {state['every']}s; {last}{fails}; {due}"


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


def _profile_home(task) -> Optional[str]:
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    try:
        profile = normalize_profile_name(task.assignee) if task.assignee else None
        return str(resolve_profile_env(profile)) if profile else None
    except (FileNotFoundError, ValueError, OSError):
        logger.debug("wake check: no profile home for %s", task.id, exc_info=True)
        return None


def _probe_env(task, db_path: str) -> dict:
    """The assignee profile's env (``served_profile_child_env``: its HERMES_HOME and its own
    secrets, never the launch profile's) plus the board pin, so ``hermes kanban show`` in a
    probe reads the card's board."""
    from agent.secret_scope import UnscopedSecretError
    from tools.environments.local import served_profile_child_env

    home = _profile_home(task)
    try:
        env = served_profile_child_env(target_home=home, inherit_credentials=True)
    except UnscopedSecretError:  # multiplex with no profile home: no credentials, not the launcher's
        logger.debug("wake check: no secret scope for %s", task.id, exc_info=True)
        env = served_profile_child_env(target_home=home, inherit_credentials=False)
    for key in ("HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_SESSION_ID", "HERMES_KANBAN_BOARD"):
        env.pop(key, None)
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_DB"] = db_path
    return env


def _run_probe(key: tuple[str, str], cmd: str, cwd: str, env: dict) -> tuple[Any, str]:
    """``(rc, output)``; rc is ``"timeout"`` when the probe outlived ``PROBE_TIMEOUT_SECONDS``."""
    try:
        proc = subprocess.Popen(
            ["bash", "-lc", cmd], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
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


def _probe_and_apply(db_path: str, task_id: str, cmd: str, cwd: str, env: dict, now: Optional[int], generation: str) -> Any:
    key = (db_path, task_id)
    try:
        rc, out = _run_probe(key, cmd, cwd, env)
        from hermes_cli import kanban_db_connect as kbc
        conn = kbc.connect(Path(db_path))
        try:
            apply_result(conn, task_id, cmd, rc, out, now=now, generation=generation)
        finally:
            conn.close()
        return rc
    except Exception:
        logger.warning("kanban wake check for %s failed to run/apply", task_id, exc_info=True)
        return None


def apply_result(conn: sqlite3.Connection, task_id: str, cmd: str, rc: Any, out: str,
                 *, generation: str, now: Optional[int] = None) -> Optional[str]:
    """Record one probe result; wake the card on success or a broken probe. Returns the event
    kind appended (``condition_met`` / ``wake_check_broken``) or None. A result for a probe
    that is no longer the card's (re-parked, unblocked) is dropped."""
    from hermes_cli import kanban_db as kb

    now = int(time.time()) if now is None else int(now)
    tail = _tail(out)
    with kb.write_txn(conn):
        state = get_state(conn, task_id)
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if (state is None or state["cmd"] != cmd or state["generation"] != generation
                or status is None or status[0] != "scheduled"):
            return None
        rc_col = rc if isinstance(rc, int) else -1
        if rc == 0:
            return _wake(conn, task_id, CONDITION_MET_EVENT, {"wake_check": cmd, "output": tail})
        if rc == 1:
            conn.execute(
                "UPDATE task_wake_checks SET last_at = ?, last_rc = 1, last_out = ?, fails = 0, "
                "next_at = ? WHERE task_id = ?", (now, tail, now + int(state["every"]), task_id))
            return None
        fails = int(state["fails"] or 0) + 1
        if fails >= BROKEN_AFTER:
            return _wake(conn, task_id, BROKEN_EVENT, {
                "wake_check": cmd, "rc": rc, "fails": fails, "output": tail,
                "note": "The wake_check failed (not exit 0/1) repeatedly. Fix the probe, then park again."})
        conn.execute(
            "UPDATE task_wake_checks SET last_at = ?, last_rc = ?, last_out = ?, fails = ?, "
            "next_at = ? WHERE task_id = ?", (now, rc_col, tail, fails, now + int(state["every"]), task_id))
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
        try:
            stale = conn.execute(
                "SELECT w.task_id FROM task_wake_checks w LEFT JOIN tasks t ON t.id = w.task_id "
                "WHERE t.status IS NOT 'scheduled'").fetchall()
        except sqlite3.OperationalError:
            return {"submitted": [], "in_flight": 0}
        if stale:
            with kb.write_txn(conn):
                for row in stale:
                    conn.execute("DELETE FROM task_wake_checks WHERE task_id = ? AND task_id NOT IN "
                                 "(SELECT id FROM tasks WHERE status = 'scheduled')", (row[0],))
        due = conn.execute(
            "SELECT w.task_id, w.cmd, w.every, w.generation FROM task_wake_checks w JOIN tasks t ON t.id = w.task_id "
            "WHERE t.status = 'scheduled' AND w.next_at <= ? ORDER BY w.next_at", (ts,)).fetchall()
        for task_id, cmd, every, generation in due:
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
                    "WHERE task_id = ? AND cmd = ? AND generation IS ? AND next_at <= ? "
                    "AND EXISTS (SELECT 1 FROM tasks WHERE id = ? AND status = 'scheduled')",
                    (ts + int(every), claim, task_id, cmd, generation, ts, task_id))
                if claimed.rowcount != 1:
                    continue
            ws = task.workspace_path
            cwd = ws if ws and os.path.isabs(ws) and os.path.isdir(ws) else str(Path.home())
            fut = _pool().submit(_probe_and_apply, db_path, task_id, cmd, cwd, _probe_env(task, db_path), now, claim)
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
    kinds = (*SCHEDULED_ENTRY_EVENTS, CONDITION_MET_EVENT, BROKEN_EVENT)
    marks = ",".join("?" for _ in kinds)
    row = conn.execute(
        f"SELECT kind, payload FROM task_events WHERE task_id = ? AND kind IN ({marks}) "
        "ORDER BY id DESC LIMIT 1", (task_id, *kinds)).fetchone()
    if row is not None and row[0] in (CONDITION_MET_EVENT, BROKEN_EVENT):
        payload = kb._json_dict(row[1])
        if row[0] == CONDITION_MET_EVENT:
            lines.append("## Woken by wake check (condition met)")
            lines.append(f"The park's wake_check `{payload.get('wake_check')}` exited 0, so this card "
                         "woke before its deadline. Its output:")
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
