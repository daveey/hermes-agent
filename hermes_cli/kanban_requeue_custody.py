"""A core-update requeue releases a card only once the run's own processes are gone or pinned. [daveey]

Incident t_01a1d85b7b4a9e6d (2026-11-14, PR #2946): hermes-sync's restart-safe requeue
(:data:`kanban_core_update_requeue.CORE_UPDATE_REQUEUE_REASON`) reclaimed run 48261 on worker-2. That
terminated only the worker process, then released the Board lease, and worker-8 claimed the retry 2 s later.
worker-2's ``hermes-core-wt ship``, started by the reclaimed run, kept running for at least 104 s more: it
pushed a head, placed a land-lane hold and logged ``review_refused`` while worker-8's own ship ran. Same class as
t_01a1b77e56b5f51c (orphaned ship plus a second ship).

At such a requeue, :func:`settle` (called by ``reclaim_task`` after the worker is terminated and before the
lease is released) lists what the run started that is still alive on this host:

* **plain** processes: every pid in the run's systemd scope (``hermes-worker-kanban-<task>-run-<n>.scope``,
  where foreground commands and plain background jobs run) and every non-persisted process-checkpoint entry of
  the card's worker sessions (``parent_session_id`` = a run's ``owner_admitted`` session). They are terminated
  as a group, TERM then KILL, before the card is released;
* **persisted** jobs (``persist_on_release``, which exist to survive exactly this pause): checkpoint entries of
  the card's worker sessions, verified by their kernel start time. They are left running, and the ``reclaimed``
  payload carries ``custody`` = ``{host, processes, until, run_uid}``.

While a custody is open, :func:`claim_refusal` (called from ``kanban_db_sync.claim_gate``, the local seam before
every Board lease) defers another host's claim with a reason naming the host, the pids and the commands; the
custody host claims as usual. The custody host's dispatch tick (:func:`sweep`) records
``requeue_custody_released`` once every custody pid has exited, or at ``until`` (the run's
``max_runtime_seconds``, 60 min when unset) terminates the jobs as groups (TERM, then KILL) and records it,
unless the custody host's own retry holds the card then (it keeps the job until that run ends). A
custody whose host never reports (the host died with its processes) stops holding ``RELEASE_GRACE_SECONDS``
after ``until``. A requeue that finds no live persisted job records no custody: today's behaviour.

Residual: a foreground command on a host with no run scope (no systemd user bus) is not found once its worker
died; the checkpoint covers background jobs only.
"""
from __future__ import annotations

import logging
import os
import signal
import sqlite3
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

RELEASED_EVENT = "requeue_custody_released"
DEFAULT_BOUND_SECONDS = 3600
RELEASE_GRACE_SECONDS = 600
TERM_GRACE_SECONDS = 5.0
SWEEP_WINDOW_SECONDS = 14 * 86400
CMD_CHARS = 160
_HAS_PROC = os.path.exists("/proc/self/stat")  # Linux; macOS has none (test seam)


# --- host identity ------------------------------------------------------------------------------

def own_host(conn: sqlite3.Connection) -> str:
    """This machine as the Board names it (``gateway_id`` of a primary board), else its local claim prefix."""
    from hermes_cli import kanban_db_sync as sync

    state = sync.sync_state(conn) or {}
    if state.get("mode") == "primary" and state.get("gateway_id"):
        return str(state["gateway_id"])
    from hermes_cli import kanban_db as kb
    return kb._host_prefix().rstrip(":")


# --- process inventory --------------------------------------------------------------------------

def _start_ticks(pid: int) -> Optional[int]:
    """Kernel start time of a running pid; None when gone or a zombie (exited, not yet reaped)."""
    from pathlib import Path
    if not _HAS_PROC:
        return _start_fingerprint_without_proc(pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8-sig")
        fields = stat[stat.rindex(")") + 2:].split()
        return None if fields[0] in ("Z", "X") else int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _start_fingerprint_without_proc(pid: int) -> Optional[int]:
    """macOS: the process registry's own fingerprint (``gateway.status.get_process_start_time``, psutil
    centiseconds), so a checkpoint's ``host_start_time`` compares equal; None when gone or a zombie."""
    try:
        import psutil  # type: ignore
        if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
            return None
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    except Exception:  # health: allow BLE001 -- psutil NoSuchProcess/AccessDenied/missing all read as not ours
        return None


def _alive(pid: Any, start: Any) -> bool:
    """Same process as recorded: pid > 1 and an exact kernel start-time match (never a bare pid)."""
    return type(pid) is int and pid > 1 and type(start) is int and _start_ticks(pid) == start


def _sessions(conn: sqlite3.Connection, task_id: str) -> set[str]:
    rows = conn.execute("SELECT payload FROM task_events WHERE kind = 'owner_admitted' AND task_id = ?", (task_id,))
    from hermes_cli import kanban_db as kb
    return {s for (p,) in rows if isinstance(s := kb._json_dict(p).get("session_id"), str) and s}


def _entry_record(entry: dict) -> dict:
    return {"pid": entry["pid"], "start": entry["host_start_time"],
            "cmd": str(entry.get("command") or "")[:CMD_CHARS], "unit": str(entry.get("systemd_unit") or "")}


def _run_scope_pids(task_id: str, run_id: Optional[int]) -> list[int]:
    """Pids in the run's systemd scope (empty without one or without a user bus). Test seam."""
    if run_id is None:
        return []
    from hermes_cli.kanban_worker_scope import _scope_pids
    return _scope_pids(f"hermes-worker-kanban-{task_id}-run-{run_id}.scope") or []


def inventory(conn: sqlite3.Connection, task_id: str, run_id: Optional[int]) -> dict[str, list[dict]]:
    """``{"persisted": [...], "plain": [...]}`` live processes the card's worker runs started on this host."""
    from hermes_cli.kanban_worker_scope import _cmdline, _is_spared, checkpoint_entries

    sessions = _sessions(conn, task_id)
    persisted: list[dict] = []
    plain: dict[int, dict] = {}
    for entry in checkpoint_entries():
        if entry.get("parent_session_id") not in sessions or entry.get("pid_scope", "host") != "host":
            continue
        if not _alive(entry.get("pid"), entry.get("host_start_time")):
            continue
        (persisted.append(_entry_record(entry)) if entry.get("persist_on_release")
         else plain.setdefault(entry["pid"], _entry_record(entry)))
    spared = {p["pid"] for p in persisted}
    for pid in _run_scope_pids(task_id, run_id):
        if pid == os.getpid() or pid in plain or _is_spared(pid, spared):
            continue
        start = _start_ticks(pid)
        if start is not None:
            plain[pid] = {"pid": pid, "start": start, "cmd": _cmdline(pid)[:CMD_CHARS], "unit": ""}
    return {"persisted": persisted, "plain": list(plain.values())}


def _signal(record: dict, sig: int, *, group: bool) -> None:
    pid = record["pid"]
    if not _alive(pid, record["start"]):
        return
    try:
        if group and os.getpgid(pid) == pid:
            os.killpg(pid, sig)  # windows-footgun: ok -- POSIX workers only; a job leads its own group
        else:
            os.kill(pid, sig)
    except OSError:
        pass


def terminate(records: list[dict], *, grace: float = TERM_GRACE_SECONDS) -> list[dict]:
    """TERM each process (its group when it leads one), wait ``grace``, then KILL survivors and stop their scope
    units. Returns the records still alive afterwards (normally none)."""
    if not records:
        return []
    for record in records:
        _signal(record, signal.SIGTERM, group=True)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(_alive(r["pid"], r["start"]) for r in records):
        time.sleep(0.1)
    kill = getattr(signal, "SIGKILL", signal.SIGTERM)
    for record in records:
        if record.get("unit"):
            from hermes_cli.kanban_worker_scope import _systemctl
            _systemctl("kill", "--signal=KILL", record["unit"])
        _signal(record, kill, group=True)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and any(_alive(r["pid"], r["start"]) for r in records):
        time.sleep(0.05)
    return [r for r in records if _alive(r["pid"], r["start"])]


# --- requeue ------------------------------------------------------------------------------------

def for_requeue(conn: sqlite3.Connection, task_id: str, reason: Optional[str]) -> dict:
    """``reclaim_task``'s hook: ``{"custody": ...}`` to merge into the ``reclaimed`` payload, or ``{}``. Only the
    restart-safe core-update requeue settles the run's processes; every other reclaim is unchanged."""
    from hermes_cli.kanban_core_update_requeue import CORE_UPDATE_REQUEUE_REASON
    if reason != CORE_UPDATE_REQUEUE_REASON:
        return {}
    row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    custody = settle(conn, task_id, int(row[0]) if row is not None and row[0] else None)
    return {"custody": custody} if custody else {}


def settle(conn: sqlite3.Connection, task_id: str, run_id: Optional[int], *,
           now: Optional[float] = None) -> Optional[dict]:
    """At a restart-safe requeue (worker already terminated, before the claim is released): end the run's plain
    processes, and return the ``custody`` payload for its live persisted jobs (None when there are none).
    Never raises: an inventory fault keeps today's release."""
    try:
        found = inventory(conn, task_id, run_id)
        survivors = terminate(found["plain"])
        held = found["persisted"] + survivors  # a plain process that outlived KILL still holds the card here
        if found["plain"]:
            log.info("kanban requeue %s: terminated run %s's plain processes %s (survivors %s)", task_id, run_id,
                     [p["pid"] for p in found["plain"]], [p["pid"] for p in survivors])
        if not held:
            return None
        row = conn.execute("SELECT max_runtime_seconds FROM tasks WHERE id = ?", (task_id,)).fetchone()
        bound = int(row[0]) if row is not None and row[0] else DEFAULT_BOUND_SECONDS
        from hermes_cli import kanban_db_sync as sync
        return {"host": own_host(conn), "run_id": run_id, "run_uid": sync.run_uid(conn, run_id),
                "processes": held, "until": int((time.time() if now is None else now) + bound),
                "terminated_plain": [p["pid"] for p in found["plain"] if p not in survivors]}
    except Exception:  # health: allow BLE001 -- a custody fault must never stop the requeue itself
        log.warning("kanban requeue %s: process custody inventory failed", task_id, exc_info=True)
        return None


# --- custody state ------------------------------------------------------------------------------

def _key(custody: dict) -> str:
    return str(custody.get("run_uid") or custody.get("run_id"))


def _custodies(conn: sqlite3.Connection, task_id: Optional[str], since: float) -> list[tuple[str, int, dict]]:
    """``(task_id, local run_id, custody)`` of open custodies (no release recorded), oldest first."""
    from hermes_cli import kanban_db as kb
    where, args = ("AND task_id = ?", (task_id,)) if task_id else ("", ())
    released = {(t, str(kb._json_dict(p).get("key"))) for t, p in conn.execute(
        f"SELECT task_id, payload FROM task_events WHERE kind = ? {where} AND created_at >= ?",
        (RELEASED_EVENT, *args, int(since)))}
    out = []
    for tid, run_id, payload in conn.execute(
            f"SELECT task_id, run_id, payload FROM task_events WHERE kind = 'reclaimed' {where} "
            "AND created_at >= ? AND payload LIKE '%\"custody\"%' ORDER BY id", (*args, int(since))):
        custody = kb._json_dict(payload).get("custody")
        if isinstance(custody, dict) and isinstance(custody.get("processes"), list) \
                and (tid, _key(custody)) not in released:
            out.append((tid, run_id, custody))
    return out


def _describe(custody: dict) -> str:
    procs = ", ".join(f"pid {p.get('pid')} ({p.get('cmd') or '?'})" for p in custody["processes"][:3]
                      if isinstance(p, dict))
    more = len(custody["processes"]) - 3
    return procs + (f" and {more} more" if more > 0 else "")


def claim_refusal(conn: sqlite3.Connection, row, *, now: Optional[float] = None) -> Optional[str]:
    """Why this host may not claim ``row`` yet, or None. Only another host is refused, and only while the
    custody is open and younger than ``until`` + :data:`RELEASE_GRACE_SECONDS`."""
    try:
        moment = time.time() if now is None else now
        open_ = _custodies(conn, row["id"], moment - SWEEP_WINDOW_SECONDS)
        if not open_:
            return None
        me = own_host(conn)
        for _tid, _run, custody in reversed(open_):
            if custody.get("host") == me or moment >= float(custody.get("until") or 0) + RELEASE_GRACE_SECONDS:
                continue
            return (f"requeue_custody: run {custody.get('run_id')}'s persisted process(es) still live on "
                    f"{custody.get('host')}: {_describe(custody)}; only {custody.get('host')} may claim until "
                    "they exit or the bound ends them")
        return None
    except Exception:  # health: allow BLE001 -- an unreadable custody keeps today's claim path
        log.warning("kanban: requeue custody check failed for %s", row["id"], exc_info=True)
        return None


def _release(conn: sqlite3.Connection, task_id: str, custody: dict, outcome: str, extra: dict) -> None:
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, RELEASED_EVENT, {
            "key": _key(custody), "host": custody.get("host"), "run_id": custody.get("run_id"),
            "outcome": outcome, **extra})


def _held_here(conn: sqlite3.Connection, task_id: str) -> bool:
    """This host's claim holds the card (the custody host re-claimed the retry): no other host can claim it, so
    the bound has nothing to prevent and must not kill a ``persist_on_release`` job the running retry relies on.
    The custody stays open; once that run ends, the sweep resumes (bound included)."""
    from hermes_cli import kanban_db as kb
    row = conn.execute("SELECT status, claim_lock FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row is not None and row["status"] == "running" and bool(row["claim_lock"]) \
        and kb._claim_is_local(row["claim_lock"], kb._host_prefix())


def sweep(conn: sqlite3.Connection, *, now: Optional[float] = None,
          terminate_fn: Optional[Callable[[list[dict]], list[dict]]] = None) -> list[str]:
    """Dispatch tick on the custody host: release custodies whose processes all exited; at ``until``, terminate
    them first. Returns the task ids released. Never raises."""
    released: list[str] = []
    try:
        moment = time.time() if now is None else now
        me = own_host(conn)
        for task_id, _run, custody in _custodies(conn, None, moment - SWEEP_WINDOW_SECONDS):
            if custody.get("host") != me:
                continue
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is not None and row["status"] in {"done", "archived"}:
                # Terminal cards cannot be claimed elsewhere. Custody no longer protects a retry,
                # and must not shorten the lifetime of its persist_on_release jobs.
                _release(conn, task_id, custody, "terminal", {"status": row["status"]})
                released.append(task_id)
                continue
            procs = [p for p in custody["processes"] if isinstance(p, dict)]
            live = [p for p in procs if _alive(p.get("pid"), p.get("start"))]
            if live and (moment < float(custody.get("until") or 0) or _held_here(conn, task_id)):
                continue  # before the bound, or the retry runs here with the job: persist_on_release holds
            extra: dict = {}
            outcome = "exited"
            if live:
                left = (terminate_fn or terminate)(live)
                if left:
                    log.warning("kanban requeue custody %s: %s survived termination", task_id,
                                [p["pid"] for p in left])
                    continue
                outcome, extra = "terminated", {"terminated": [p["pid"] for p in live]}
            _release(conn, task_id, custody, outcome, extra)
            released.append(task_id)
    except Exception:  # health: allow BLE001 -- the sweep must never stop a dispatch tick
        log.warning("kanban: requeue custody sweep failed", exc_info=True)
    return released
