"""Stale-claim helpers shared by ``kanban_db.release_stale_claims`` and the dispatcher sweeps.

Moved out of ``kanban_db`` / ``kanban_db_dispatch`` (file-size caps); both re-export them.
"""

from __future__ import annotations

import sqlite3
from typing import Optional


def _extend_live_stale_claim(conn: sqlite3.Connection, row: sqlite3.Row, now: int) -> None:
    """TTL-expired claim whose host-local worker is alive: extend instead of
    reclaiming (``claim_extended`` event). CAS on the same expired lock so a
    concurrent reclaimer wins cleanly."""
    from hermes_cli import kanban_db as _kb

    new_expires = now + _kb._resolve_claim_ttl_seconds()
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' "
            "  AND claim_lock IS ? "
            "  AND claim_expires IS NOT NULL "
            "  AND claim_expires < ?", (new_expires, row["id"], row["claim_lock"], now),
        )
        if cur.rowcount != 1:
            return
        run_id = _kb._extend_run_claim(conn, row["id"], new_expires)
        _kb._append_event(
            conn, row["id"], "claim_extended",
            {
                "reason": "pid_alive",
                "worker_pid": int(row["worker_pid"]),
                "claim_lock": row["claim_lock"],
                "claim_expires_was": _kb._to_epoch(row["claim_expires"]),
                "claim_expires_now": new_expires,
                "last_heartbeat_at": _kb._to_epoch(row["last_heartbeat_at"]),
            },
            run_id=run_id,
        )


def _defer_reclaim_for_live_worker(
    conn: sqlite3.Connection,
    task_id: str,
    claim_lock: Optional[str],
    now: int,
    termination: dict,
    *,
    reason: str,
) -> None:
    """Hold a claim whose worker survived termination instead of releasing it.

    Extends ``claim_expires`` by ``RECLAIM_DEFER_GRACE_SECONDS`` so the task
    stays ``running`` (no duplicate spawn) and records ``reclaim_deferred``.
    The next tick retries the kill; not spawning a duplicate is what lets the
    throttled worker finally die.
    """
    from hermes_cli import kanban_db as _kb

    grace = now + _kb.RECLAIM_DEFER_GRACE_SECONDS
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock IS ?",
            (grace, task_id, claim_lock),
        )
        if cur.rowcount != 1:
            return
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (grace, run_id))
        payload = {"reason": reason, "claim_lock": claim_lock, "claim_expires_now": grace}
        payload.update(termination)
        _kb._append_event(conn, task_id, "reclaim_deferred", payload, run_id=run_id)


def _exit_booked_transient(conn: sqlite3.Connection, row: sqlite3.Row, host_local: bool, started_at) -> bool:
    """[daveey] True when a stale claim's host-local worker is already dead and its run's authority
    recorded the transient exit (``KANBAN_RATE_LIMIT_EXIT_CODE``: requeue without a failure) under
    this claim. ``release_stale_claims`` then leaves the run to the crash sweep of the same dispatcher
    pass, which books exactly that exit. Reclaiming it here instead closed runs 10712/10714 as
    ``lease_lost`` failures after their workers exited 75 in a networkless DarkWake: the stale sweep
    runs first, and a lease that lapsed during the sleep is all it sees. A live worker, a worker with
    no booking or another exit code, and a remote claim are untouched (the reclaim proceeds)."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli.kanban_owner_recovery import managed_run_exit_code

    pid = row["worker_pid"]
    if not host_local or not pid or _kb._worker_alive(pid, started_at):
        return False
    try:
        code = managed_run_exit_code(conn, row["id"], int(pid), row["claim_lock"])
    except (sqlite3.Error, TypeError, ValueError):
        return False
    return code == _kb.KANBAN_RATE_LIMIT_EXIT_CODE


def _stale_claim_kept(conn: sqlite3.Connection, row: sqlite3.Row, now: int, *, live: bool, host_local: bool,
                      started_at) -> bool:
    """True when ``release_stale_claims`` keeps this expired claim: a live host-local worker the
    Board still lets us hold has it extended (``_extend_live_stale_claim``), and a dead worker whose
    transient exit is booked is left to the crash sweep (``_exit_booked_transient``)."""
    if live:
        _extend_live_stale_claim(conn, row, now)
        return True
    return _exit_booked_transient(conn, row, host_local, started_at)
