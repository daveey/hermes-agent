"""Kanban claim publication boundary for the POSIX updater fence."""
import contextlib
import logging
from pathlib import Path


def claim_and_open_run(conn, task_id, source_status, lock, expires, now, *, event_extra=None, gate=None):
    """CAS to running and publish its run/event. Caller owns the fenced transaction."""
    from hermes_cli import kanban_db as kb

    # Activity belongs to this run, not the previous worker. Seed it atomically
    # so the first expired TTL cannot treat a freshly woken worker as wedged.
    cur = conn.execute(
        """
        UPDATE tasks
           SET status = 'running', human_request = NULL, claim_lock = ?,
               claim_expires = ?, started_at = COALESCE(started_at, ?),
               last_heartbeat_at = ?
         WHERE id = ? AND status = ? AND claim_lock IS NULL
        """,
        (lock, expires, now, now, task_id, source_status),
    )
    if cur.rowcount != 1:
        return None
    run_uid = kb._sync.stage_claim(conn, task_id, gate)
    trow = conn.execute(
        "SELECT assignee, max_runtime_seconds, current_step_key FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    fields = (trow["assignee"] if trow else None, trow["current_step_key"] if trow else None,
              lock, expires, trow["max_runtime_seconds"] if trow else None, now)
    # The lease's run uid goes into the row itself, so the run is the one the Board granted even when no
    # capture trigger is installed (see ``stage_claim``). A Board echo of this claim pulled before this txn left
    # a ``pending_sync`` placeholder with that uid: that row becomes the run, never a second one. [daveey]
    placeholder = conn.execute(
        "SELECT id FROM task_runs WHERE uid = ? AND task_id = ? AND status = 'pending_sync'", (run_uid, task_id),
    ).fetchone() if run_uid else None
    if placeholder is not None:
        run_id = placeholder[0]
        conn.execute(
            "UPDATE task_runs SET profile = ?, step_key = ?, status = 'running', claim_lock = ?, claim_expires = ?, "
            "max_runtime_seconds = ?, started_at = ? WHERE id = ?", (*fields, run_id),
        )
    else:
        uid_col, uid_val = (", uid", (run_uid,)) if run_uid else ("", ())
        run_cur = conn.execute(
            f"""
            INSERT INTO task_runs (
                task_id, profile, step_key, status, claim_lock, claim_expires, max_runtime_seconds, started_at{uid_col}
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?{', ?' if run_uid else ''})
            """,
            (task_id, *fields, *uid_val),
        )
        run_id = run_cur.lastrowid
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
    kb._append_event(
        conn, task_id, "claimed",
        {"lock": lock, "expires": expires, "run_id": run_id, **(event_extra or {})}, run_id=run_id,
    )
    return run_id


def admission_hold_set():
    """[daveey] Lock-free early read of the updater hold, for callers to check BEFORE side effects that
    precede ``claim_transaction`` (the Board-service lease). Advisory only: ``claim_transaction`` still
    re-checks under admission.lock, so a hold set after this read is still honoured. An unreadable
    fence reads as "not set" here and is decided by that authoritative check."""
    from hermes_constants import get_default_hermes_root
    from hermes_cli.admission_fence import is_held, SUPPORTED

    if not SUPPORTED:
        return False
    try:
        return is_held(Path(get_default_hermes_root()) / "core-update")
    except OSError:
        return False


@contextlib.contextmanager
def claim_transaction(conn):
    """Fence check and real SQL commit share admission.lock (never a savepoint).

    Board storage may be overridden independently of the installation. Use the
    process's installation root, not the board path. Hooks run after both locks.
    """
    from hermes_constants import get_default_hermes_root
    from hermes_cli.admission_fence import admission_lock, is_held, HOLD_MESSAGE, SUPPORTED
    from hermes_cli.kanban_db import write_txn

    if conn.in_transaction:
        raise RuntimeError("Kanban claim cannot run inside an existing transaction")
    if not SUPPORTED:
        with write_txn(conn):
            yield True
        return
    # BEGIN IMMEDIATE can wait through SQLite's busy timeout and retries. Do
    # that before taking the installation-wide flock, so a busy board cannot
    # stall updater holds or unrelated reservations. Never wait for admission
    # while owning the DB writer lock: an unavailable flock defers this claim.
    # The stack deliberately OUTLIVES write_txn: accepted claims must COMMIT
    # (including retries), or roll back on failure, before releasing admission.
    with contextlib.ExitStack() as stack:
        with write_txn(conn):
            try:
                root = stack.enter_context(admission_lock(get_default_hermes_root(), timeout=0))
                held = is_held(root)
            except (OSError, TimeoutError):
                logging.getLogger(__name__).warning("Update admission unavailable; deferring Kanban claim", exc_info=True)
                yield False
                return
            if held:
                logging.getLogger(__name__).info(HOLD_MESSAGE)
                yield False
            else:
                yield True
