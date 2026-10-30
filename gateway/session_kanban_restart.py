"""Owner-side disposition of a Kanban turn that cooperatively accepted a runtime Stop."""
import json
from pathlib import Path


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
            'claim_lock': context['claim_lock'], 'exit_code': KANBAN_OWNER_RESTART_EXIT_CODE,
            'last_output': 'Kanban attempt interrupted by gateway restart/shutdown; requeued uncounted.',
            'owner_restart': True}, run_id=context['run_id'])
