"""Clear this host's claim when the Board applies the failure release this host queued. [daveey]

On a primary board the dead-worker sweep queues a run-scoped ``release`` command (``kanban_failure_release``) and
leaves the local row alone; the Board feed later moves the card off ``running`` (``ready`` for a retry, ``triage`` or
``blocked`` for a give-up). Claim columns are local-only, so the feed never clears them: the card kept the dead
worker's ``claim_lock``/``worker_pid``. A retry waited out ``ORPHAN_CLAIM_GRACE_SECONDS`` for the leftover sweep, and a
give-up to ``triage`` kept its claim forever, so the creator could not archive it (worker-7 t_01a1d62545843356,
runs 2440/2462). The status move and the claim release now commit in the same feed transaction.
"""
from __future__ import annotations

import sqlite3


def settle_retained(conn: sqlite3.Connection, task_id: str, run_uid: str) -> bool:
    """Exact run/claim CAS also works when the feed already cleared current_run_id."""
    from hermes_cli.kanban_retain_transport import confirmed_command
    run = conn.execute('SELECT id,claim_lock FROM task_runs WHERE task_id=? AND uid=?', (task_id, run_uid)).fetchone()
    if not run or not run['claim_lock'] or not confirmed_command(conn, task_id, run_uid):
        return False
    cur = conn.execute("UPDATE tasks SET claim_lock=NULL,claim_expires=NULL,worker_pid=NULL,worker_started_at=NULL,last_heartbeat_at=NULL "
                       "WHERE id=? AND status='triage' AND claim_lock=? AND (current_run_id IS NULL OR current_run_id=?)",
                       (task_id, run['claim_lock'], run['id']))
    return cur.rowcount == 1


def release_on_feed(conn: sqlite3.Connection, task_id: str, old, new_status) -> bool:
    """Called inside the feed apply, after the task row update. ``old`` is the local row before it.

    Clears the claim only when the card left ``running``, the claim is this host's, the claimed run has a failure
    command this host queued (so its worker was already judged gone), and that worker is not alive. The CAS matches
    the exact claim values read, so a claim taken since is never touched."""
    if old is None or old["status"] != "running" or not new_status or new_status == "running":
        return False
    lock, run_id = old["claim_lock"], old["current_run_id"]
    from hermes_cli import kanban_db as kb
    if not lock or run_id is None or not kb._claim_is_local(lock, kb._host_prefix()):
        return False
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_failure_commands'").fetchone():
        return False
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_failure_release import queued
    uid = sync.run_uid(conn, run_id)
    from hermes_cli.kanban_retain_transport import confirmed_command
    if uid and new_status == 'triage' and confirmed_command(conn, task_id, uid):
        cur = conn.execute("UPDATE tasks SET claim_lock=NULL,claim_expires=NULL,worker_pid=NULL,worker_started_at=NULL,last_heartbeat_at=NULL "
                           "WHERE id=? AND status=? AND claim_lock IS ? AND worker_pid IS ? AND worker_started_at IS ?",
                           (task_id, new_status, lock, old['worker_pid'], old['worker_started_at']))
        return cur.rowcount == 1
    if not uid or not queued(conn, run_id) or conn.execute("SELECT 1 FROM kanban_failure_commands WHERE run_uid = ? AND task_id = ?",
                               (uid, task_id)).fetchone() is None:
        return False
    pid = old["worker_pid"]
    from hermes_cli.kanban_db_dispatch import _worker_alive
    if pid and _worker_alive(pid, old["worker_started_at"]):
        return False
    cur = conn.execute(
        "UPDATE tasks SET claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
        "last_heartbeat_at = NULL WHERE id = ? AND status = ? AND claim_lock IS ? AND worker_pid IS ?",
        (task_id, new_status, lock, pid),
    )
    if cur.rowcount == 1:
        kb._log.info("kanban: feed applied the queued release of %s (%s): cleared local claim %s",
                     task_id, new_status, lock)
    return cur.rowcount == 1
