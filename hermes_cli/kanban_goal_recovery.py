"""Kernel-only goal-unachievable receipts and run-owned triage parks.

Worker block metadata is not judge evidence. Only the judge entry points call
``park``; its snapshot and ownership checks run in the same transaction as block.
"""
from dataclasses import dataclass
from hermes_cli.kanban_goal_receipt import digest, subject

EVENT = 'goal_unachievable'


@dataclass(frozen=True)
class GoalRejection:
    snapshot: str


def park(conn, task, reason, *, expected_run_id):
    from hermes_cli import kanban_db as kb
    if not task or expected_run_id is None:
        return False
    return kb.block_task(conn, task.id, kind='transient', reason=reason,
                         expected_run_id=expected_run_id,
                         goal_rejection=GoalRejection(digest(subject(task, reason))))


def record_block(conn, task_id, proof, reason, expected_run_id, kind, human_request):
    """Validate an optional rejection before the enclosing block transition."""
    if proof is None:
        return True
    return (kind == 'transient' and human_request is None
            and record(conn, task_id, proof, reason, expected_run_id))


def record(conn, task_id, proof, reason, expected_run_id):
    """Called only inside block_task's transaction, before any transition."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_continuation import HUMAN_BLOCK_KINDS
    current = kb.get_task(conn, task_id)
    if (not current or not current.goal_mode or current.status != 'running'
            or expected_run_id is None or current.current_run_id != expected_run_id
            or current.human_request is not None or current.block_kind in HUMAN_BLOCK_KINDS
            or not isinstance(proof, GoalRejection)
            or proof.snapshot != digest(subject(current, reason))):
        return False
    kb._append_event(conn, task_id, EVENT,
                     {'subject': subject(current, reason), 'snapshot': proof.snapshot},
                     run_id=expected_run_id)
    return True


def valid_receipt(conn, task_id, run_id, reason, *, before=None):
    """Match the kernel receipt to this run, goal, and exact judge reason."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_resume_exec import _payload
    from hermes_cli import kanban_recovery_history as history

    task = kb.get_task(conn, task_id)
    if not task or not task.goal_mode or run_id is None:
        return False
    row = conn.execute(
        'SELECT * FROM task_events WHERE task_id=? AND run_id=? AND kind=? '
        'ORDER BY id DESC LIMIT 1', (task_id, run_id, EVENT)).fetchone()
    if not row:
        return False
    if before is not None:
        block = conn.execute('SELECT * FROM task_events WHERE id=?', (before,)).fetchone()
        if block is None or not history.receipt_precedes(conn, row, block):
            return False
    data = _payload(row['payload'])
    expected = {**subject(task, reason), 'run_id': run_id, 'status': 'running'}
    return data.get('subject') == expected and data.get('snapshot') == digest(expected)


def recovery_assignee(task):
    """Reuse a configured, capable profile; never manufacture a privileged worker."""
    from pathlib import Path
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_backend import config_exists
    from hermes_cli.profiles import resolve_profile_env
    from hermes_cli.kanban_capabilities import validate_assignment, queue_assignees

    cfg = load_config_readonly()
    candidates = [task.assignee, (cfg.get('kanban') or {}).get('orchestrator_profile'), 'default']
    queues = queue_assignees()
    for name in dict.fromkeys(candidates):
        if not isinstance(name, str) or not name or name in queues:
            continue
        try:
            if not config_exists(Path(resolve_profile_env(name)) / 'config.yaml'):
                continue
            validate_assignment(name, requires=['terminal', 'file', 'kanban'], workspace_kind='scratch')
        except (ValueError, OSError):
            continue
        return name
    return None


def current_park(conn, task_id):
    """Keep judged goals out of generic triage re-entry, even after recovery ends."""
    from hermes_cli.kanban_resume_exec import _payload
    from hermes_cli import kanban_recovery_history as history
    if history.synced(conn):
        row = history.latest(conn, task_id, ('blocked',))
        return bool(row and _payload(row['payload']).get('rekind_reason') == EVENT)
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",
        (task_id,)).fetchone()
    return bool(row and _payload(row[0]).get('rekind_reason') == EVENT)
