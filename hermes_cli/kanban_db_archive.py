"""Task archive transition and post-commit cleanup."""
from __future__ import annotations

import sqlite3
from typing import Callable, Optional

from hermes_cli.kanban_outcome_refresh import refreshes_origin_outcomes


@refreshes_origin_outcomes
def archive_task(conn: sqlite3.Connection, task_id: str, *, signal_fn=None, via: Optional[str] = None,
                 event_extra: Optional[dict] = None, comment: Optional[tuple[str, str]] = None,
                 precondition: Optional[Callable[[sqlite3.Connection], Optional[str]]] = None) -> bool:
    """Archive a task; a *running* task's host-local worker is terminated.

    Clearing ``worker_pid`` in the DB alone left the OS process running past its
    own archive — it kept executing (and pushing work) against a task nothing
    tracked anymore (#76196). Snapshot pid+claim inside the archive txn so the
    kill is contingent on THIS caller winning the archive transition (a losing
    concurrent archiver must never signal the pid); the kill itself runs after
    commit — ``_poll_worker_exit`` can wait ~5 s and must not hold the write
    lock. Post-release kill is safe here because ``archived`` is terminal: no
    dispatcher can spawn a duplicate worker off the released claim. The
    termination outcome lands as its own ``archive_worker_termination`` event so
    the ``archived`` event stays atomic with the status flip. Optional ``precondition``
    runs inside that transaction; a nonempty refusal leaves no transition or comment.
    ``comment`` and ``event_extra`` commit with the archive, before post-commit cleanup.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_reason_comment import reason_comment

    with kb.write_txn(conn), reason_comment(conn, task_id, comment) as noted:
        if precondition is not None and precondition(conn):
            return False
        row = conn.execute(
            "SELECT status, human_request, claim_lock, worker_pid, worker_started_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return False
        was_running = row["status"] == "running"
        prev_pid, prev_lock, prev_started = row["worker_pid"], row["claim_lock"], row["worker_started_at"]
        cur = conn.execute(
            "UPDATE tasks SET status = 'archived', human_request = NULL, "
            "    claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND status != 'archived'", (task_id,),
        )
        if cur.rowcount != 1:
            return False
        # Archived mid-run (dashboard): close the run so history isn't orphaned.
        run_id = kb._end_run(
            conn, task_id, outcome="reclaimed", status="reclaimed",
            summary="task archived with run still active",
        )
        from hermes_cli import kanban_run_end_lease
        kanban_run_end_lease.note(conn, task_id, run_id)  # [daveey] free the Board lease after commit
        # [daveey] via: archived outside the creator scope; event_extra: e.g. a forwarded run (kanban_owner_archive_relay)
        payload = {"source_status": row["status"], **({"via": via} if via else {}), **(event_extra or {})}
        kb._append_event(conn, task_id, "archived", payload, run_id=run_id)
        # Preserve a terminal receipt for the ask before archive removes its subscription.
        # This is withdrawal, never a user's answer or successful task completion.
        if row["status"] == "blocked" and row["human_request"]:
            from hermes_cli.kanban_withdrawal_recipients import snapshot
            kb._append_event(conn, task_id, "request_withdrawn",
                             {"notice_version": 1, "recipients": snapshot(conn, task_id)}, run_id=run_id)
        # [daveey] Same txn as the flip: a crash before forward_archive below still leaves the cancellation
        # for the dispatcher sweep (kanban_placement_forward.sweep_unresolved_archives).
        from hermes_cli.kanban_placement_forward import record_cancel_intent
        record_cancel_intent(conn, task_id, "archived")
        noted.keep()
    if was_running:
        termination = kb._terminate_reclaimed_worker(prev_pid, prev_lock, signal_fn=signal_fn, started_at=prev_started)
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "archive_worker_termination", termination, run_id=run_id)
    # ``archived`` parents no longer block children; promote them now.
    kb.recompute_ready(conn)
    # Reap the workspace on archive too (never-completed tasks kept it forever).
    kb._cleanup_workspace(conn, task_id)
    # [daveey] A card placement forwarded to another gateway: archive that copy too (kanban_placement_forward).
    from hermes_cli.kanban_placement_forward import forward_archive
    forward_archive(conn, task_id)
    return True



def _install_outcome_refresh() -> None:  # reads the module-level ``_kb`` bound below
    """[daveey] Wrap the block/unblock/complete transitions (defined in the kanban_db facade, which only imports
    this tail module) so each refreshes the origin chats' stored outcomes after commit. Idempotent."""
    for name in ("block_task", "unblock_task", "complete_task"):
        fn = getattr(_kb, name)
        if not getattr(fn, "_refreshes_outcomes", False):
            setattr(_kb, name, refreshes_origin_outcomes(fn))


from hermes_cli import kanban_db as _kb  # at the tail: kanban_db imports this module back

_install_outcome_refresh()
