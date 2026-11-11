"""Owner-side disposition of a Kanban turn interrupted by a runtime Stop.

[daveey] A turn the restart Stop ended cleanly (the managed child returned its interrupted result, so
its transcript and tool results are persisted) is a *checkpoint*, not a loss: the run keeps its id,
claim, submitter and session, and the next owner continues it (``gateway.session_kanban_continue``).
The booking is still the uncounted owner-restart exit, now marked ``continuable`` with a one-time
``checkpoint`` token, plus a ``restart_checkpoint`` event (``reason: core_update_pause``) naming the
admission that stopped. A submitter that cannot continue (an old client, no owner within its wait, an
owner refusing the continuation) exits on that same booking: today's uncounted requeue.
A lost child (no clean result: the admission settles ``unknown``) is never continued: its booking
carries no checkpoint, so the run is requeued as before.
"""
import asyncio
from functools import partial
import json
import logging
from pathlib import Path
import time
import uuid

logger = logging.getLogger(__name__)

CHECKPOINT_EVENT = 'restart_checkpoint'
CHECKPOINT_REASON = 'core_update_pause'  # the Desktop update pause's marker reason (tui_gateway.update_pause)


def _restart_stop(cause) -> bool:
    from gateway.run import _INTERRUPT_REASON_GATEWAY_RESTART, _INTERRUPT_REASON_GATEWAY_SHUTDOWN
    return cause in (_INTERRUPT_REASON_GATEWAY_RESTART, _INTERRUPT_REASON_GATEWAY_SHUTDOWN)


async def close_and_book_restart_loss(authority, policy, worker):
    """Return final cleanup; a restart-lost child is closed and booked before unknown publishes.

    Closing first prevents its late130 from overwriting76. Other losses retain ordinary cleanup.
    The returned no-op means the caller's finally never closes that same child twice. The child was
    lost (no clean result), so this booking is never a continuation checkpoint.
    """
    if not policy.kanban_json or not _restart_stop(worker.stop_cause):
        return worker.close
    await asyncio.to_thread(worker.close)
    logger.info('Kanban restart: not checkpointing the turn (managed worker lost before its result); '
                'the run is requeued uncounted')
    await book_restart_interruption(authority, policy)
    return lambda: None


async def book_restart_interruption(authority, policy, checkpoint=None):
    """Best-effort booking: a failed board write must never skip settlement of the admission.

    ``checkpoint`` (``{'session_id', 'request_id', 'admission_id', 'cause'}``) marks a turn that stopped
    cleanly at the restart: the booking is then continuable by the next owner."""
    from gateway.session_runtime_workers import tracked_write
    try:
        await tracked_write(authority, partial(record_restart_interruption, policy, checkpoint=checkpoint))
    except Exception:
        logger.warning('Kanban restart-loss booking failed; settling without it', exc_info=True)


def record_restart_interruption(policy, checkpoint=None):
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
        payload = {'pid': task.worker_pid, 'claim_lock': context['claim_lock'],
                   'exit_code': KANBAN_OWNER_RESTART_EXIT_CODE,
                   'last_output': 'Kanban attempt interrupted by gateway restart/shutdown; requeued uncounted.',
                   'owner_restart': True}
        if checkpoint:
            token = uuid.uuid4().hex
            payload.update(continuable=True, checkpoint=token, last_output=(
                'Kanban turn checkpointed at a gateway restart/shutdown; the next owner continues it '
                '(requeued uncounted if it cannot).'))
            kb._append_event(conn, task.id, CHECKPOINT_EVENT, {
                'reason': CHECKPOINT_REASON, 'checkpoint': token, 'at': int(time.time()),
                'claim_lock': context['claim_lock'],
                **{k: checkpoint.get(k) for k in ('session_id', 'request_id', 'admission_id', 'cause')}},
                run_id=context['run_id'])
        kb._append_event(conn, task.id, 'worker_result', payload, run_id=context['run_id'])
