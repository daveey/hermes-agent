"""Manual reclaims, including the updater's failure-neutral pause."""
from __future__ import annotations

from hermes_cli.kanban_core_update_requeue import CORE_UPDATE_REQUEUE_REASON
from hermes_cli.kanban_update_reclaim_intent import fence


def reclaim_task(conn, task_id, *, reason=None, signal_fn=None, attribution=None):
    from hermes_cli import kanban_db as kb

    row = conn.execute(
        "SELECT status, current_run_id, claim_lock, worker_pid, worker_started_at FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    if row is None or (row["status"] != "running" and row["claim_lock"] is None):
        return False
    if kb._sync.refuse_foreign_reclaim(conn, task_id):
        return False
    with fence(conn, task_id, row, reason, attribution) as admitted:
        if not admitted:
            return False
        return _reclaim(conn, task_id, row, reason, signal_fn, attribution or {})


def _reclaim(conn, task_id, row, reason, signal_fn, attribution):
    from hermes_cli import kanban_db as kb

    prev_lock = row["claim_lock"]
    termination = kb._terminate_reclaimed_worker(
        row["worker_pid"], prev_lock, signal_fn=signal_fn, started_at=row["worker_started_at"])
    # Validated explicit-operator attribution (kanban_db.reclaim_task), absent on legacy reclaims.
    custody = {**kb._requeue_custody.for_requeue(conn, task_id, reason), **attribution}
    with kb.write_txn(conn):
        retry_status = kb._retry_status_for_run(conn, task_id)
        cur = conn.execute(
            "UPDATE tasks SET status=?, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, worker_started_at=NULL "
            "WHERE id=? AND status IN ('running', 'ready', 'blocked') "
            "AND claim_lock IS ? AND current_run_id IS ? AND worker_pid IS ? AND worker_started_at IS ?",
            (retry_status, task_id, prev_lock, row["current_run_id"], row["worker_pid"], row["worker_started_at"]),
        )
        if cur.rowcount != 1:
            return False
        run_id = kb._record_reclaim(
            conn, task_id, termination,
            error=f"manual_reclaim: {reason}" if reason else f"manual_reclaim lock={prev_lock}",
            payload={"manual": True, "reason": reason, "prev_lock": prev_lock, "retry_status": retry_status, **custody},
        )
    kb._sync.release_lease(conn, task_id, kb._sync.run_uid(conn, run_id))
    # Automatic pauses are not operator forgiveness of earlier, unrelated failures.
    if reason != CORE_UPDATE_REQUEUE_REASON:
        kb._clear_failure_counter(conn, task_id)
    return True
