"""Owner-side disposition of a Kanban turn interrupted by a runtime Stop."""
import asyncio
from functools import partial
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def close_and_book_restart_loss(authority, policy, worker):
    """Return final cleanup; a restart-lost child is closed and booked before unknown publishes.

    Closing first prevents its late130 from overwriting76. Other losses retain ordinary cleanup.
    The returned no-op means the caller's finally never closes that same child twice.
    """
    from gateway.run import _INTERRUPT_REASON_GATEWAY_RESTART, _INTERRUPT_REASON_GATEWAY_SHUTDOWN
    if (not policy.kanban_json or worker.stop_cause not in
            (_INTERRUPT_REASON_GATEWAY_RESTART, _INTERRUPT_REASON_GATEWAY_SHUTDOWN)):
        return worker.close
    await asyncio.to_thread(worker.close)
    await book_restart_interruption(authority, policy)
    return lambda: None


async def book_restart_interruption(authority, policy):
    """Best-effort booking: a failed board write must never skip settlement of the admission."""
    from gateway.session_runtime_workers import tracked_write
    try:
        await tracked_write(authority, partial(record_restart_interruption, policy))
    except Exception:
        logger.warning('Kanban restart-loss booking failed; settling without it', exc_info=True)


def record_restart_interruption(policy):
    """Supersede the child's interrupted exit with an uncounted owner-restart exit.

    Called before admission settlement, including a Stop before worker bootstrap. Fence on
    BOTH the task and run claim: an old owner's completion must never touch a newer attempt.
    Unlike the child's writer, the owner is not the worker_bound PID.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_db_dispatch_exit_code import KANBAN_OWNER_RESTART_EXIT_CODE
    context = json.loads(policy.kanban_json)
    with connect_closing(Path(context['db'])) as conn, kb.write_txn(conn):
        task = kb.get_task(conn, context['task_id'])
        run = conn.execute('SELECT status,claim_lock,task_id FROM task_runs WHERE id=?',
                           (context['run_id'],)).fetchone()
        if (task is None or task.status != 'running' or task.current_run_id != context['run_id']
                or task.claim_lock != context['claim_lock'] or run is None
                or tuple(run) != ('running', context['claim_lock'], context['task_id'])):
            return
        kb._append_event(conn, task.id, 'worker_result', {
            'pid': task.worker_pid, 'claim_lock': context['claim_lock'], 'exit_code': KANBAN_OWNER_RESTART_EXIT_CODE,
            'last_output': 'Kanban attempt interrupted by gateway restart/shutdown; requeued uncounted.',
            'owner_restart': True}, run_id=context['run_id'])
