"""Transaction-owned, exact-run dead-worker closure primitives. [daveey]

Callers supply policy classification and own the enclosing transaction. No
session-store access, process signals, commits, or observer callbacks here.
"""
from __future__ import annotations


def close_dead_run(conn, row, dead):
    """CAS the original claim/run, close it, and append its diagnostic once.

    Returns ``(run_id, retry_status)`` or None if another writer won. Failure
    accounting belongs in the same transaction; effects belong after commit.
    """
    from hermes_cli import kanban_db as kb

    if not conn.in_transaction:
        raise RuntimeError('close_dead_run requires a caller-owned transaction')
    run_id = row['current_run_id']
    run = conn.execute(
        'SELECT status, ended_at FROM task_runs WHERE id=? AND task_id=?',
        (run_id, row['id']),
    ).fetchone()
    if run is None or run['status'] != 'running' or run['ended_at'] is not None:
        return None
    retry_status = kb._retry_status_for_run(conn, row['id'])
    cur = conn.execute(
        'UPDATE tasks SET status=?, claim_lock=NULL, claim_expires=NULL, '
        'worker_pid=NULL, worker_started_at=NULL '
        "WHERE id=? AND status='running' AND current_run_id=? "
        'AND worker_pid IS ? AND worker_started_at IS ? AND claim_lock IS ?',
        (retry_status, row['id'], run_id, row['worker_pid'],
         row['worker_started_at'], row['claim_lock']),
    )
    if cur.rowcount != 1:
        return None
    dead.event_payload['retry_status'] = retry_status
    kb._end_run(conn, row['id'], outcome=dead.run_outcome, status=dead.run_outcome,
                error=dead.error_text, metadata=dict(dead.event_payload))
    kb._append_event(conn, row['id'], dead.event_kind, dead.event_payload, run_id=run_id)
    return run_id, retry_status
