"""Bounded, durable supervisor handoff for worker-local assignment failures.

Called inside block_task's transaction, after its run-ownership CAS. A supervisor
is a normal ready card, never a human blocker or an unobservable notification.
"""
from __future__ import annotations

import re
from pathlib import Path

from hermes_cli.kanban_capabilities import CapabilityMismatchError, validate_assignment

_SUPERVISOR_PREFIX = 'assignment-repair:'
_HUMAN_ACCESS = re.compile(
    r'\b(credential\w*|password\w*|secret\w*|token\w*|oauth|login|log[ -]?in|'
    r'sign[ -]?in|authenticat\w*|authoriz\w*|api[ _-]?key|2fa|mfa|approval|consent|'
    r'payment|billing|policy[ -]+decision)\b', re.IGNORECASE,
)


# Full-match a deliberately small vocabulary: policy drift alone must never
# absorb an unrelated human decision or a mixed tool-and-human request.
_WORKER_MISMATCH = re.compile(
    r'(?:(?:worker|profile|assignee) (?:lacks|is missing|has no) '
    r'(?:terminal|file|git|repo|repository|host_repo)(?: tools?| access)?|'
    r'(?:missing|no) (?:terminal|file|git|repo|repository)(?: tools?| access)?|'
    r'(?:wrong|incompatible) (?:worker|profile|assignee)|'
    r'(?:worker|profile|assignee) (?:capability )?mismatch)[.!]?', re.IGNORECASE,
)


class AssignmentRoutingError(ValueError):
    """No valid repair can be scheduled; callers must surface this failure."""

    code = 'no_capable_supervisor'


def classify(task, kind, reason):
    """Require both a narrow worker-local reason and independent policy evidence."""
    if kind == 'assignment' and _HUMAN_ACCESS.search(reason or ''):
        return 'capability', 'human_access_request'
    if (kind != 'capability' or _HUMAN_ACCESS.search(reason or '')
            or not _WORKER_MISMATCH.fullmatch((reason or '').strip())):
        return kind, None
    try:
        validate_assignment(task.assignee, requires=task.requires,
                            workspace_kind=task.workspace_kind,
                            workspace_path=task.workspace_path)
    except CapabilityMismatchError as exc:
        # Unreadable policy is not evidence that the worker lacks tools.
        if exc.__cause__ is None:
            return 'assignment', 'profile_capability_mismatch'
    return kind, None


def _supervisor(task):
    from hermes_cli.config import load_config_readonly
    from hermes_cli.profiles import resolve_profile_env

    cfg = load_config_readonly()
    candidates = [cfg.get('kanban', {}).get('orchestrator_profile'), task.created_by]
    for name in dict.fromkeys(candidates):
        if not isinstance(name, str) or not name or name == task.assignee:
            continue
        try:
            # Unconstrained scratch validation intentionally skips profile IO;
            # routing still needs an existing configured profile, never a label.
            if not (Path(resolve_profile_env(name)) / 'config.yaml').is_file():
                continue
            required = validate_assignment(name, requires=[*(task.requires or []), 'kanban'],
                                           workspace_kind=task.workspace_kind,
                                           workspace_path=task.workspace_path)
        except (ValueError, OSError):
            continue
        return name, required
    return None, []


def validate_repair_completion(conn, task_id):
    """A completed repair must not release an invalid assignment after policy drift."""
    from hermes_cli import kanban_db as kb

    row = conn.execute('SELECT idempotency_key FROM tasks WHERE id=?', (task_id,)).fetchone()
    key = row[0] if row else None
    if not key or not key.startswith(_SUPERVISOR_PREFIX):
        return
    original = kb.get_task(conn, key.removeprefix(_SUPERVISOR_PREFIX))
    if original is None or original.assignee is None:
        raise AssignmentRoutingError('Assignment repair has no assigned original task')
    validate_assignment(original.assignee, requires=original.requires,
                        workspace_kind=original.workspace_kind,
                        workspace_path=original.workspace_path)


def route(conn, task, payload):
    """Create at most one repair card per original, atomically with its wait edge."""
    from hermes_cli import kanban_db as kb

    key = _SUPERVISOR_PREFIX + task.id
    own_key = conn.execute('SELECT idempotency_key FROM tasks WHERE id=?', (task.id,)).fetchone()[0]
    previous = conn.execute('SELECT id FROM tasks WHERE idempotency_key=?', (key,)).fetchone()
    if (own_key or '').startswith(_SUPERVISOR_PREFIX) or previous:
        raise AssignmentRoutingError(
            'supervisor_route_exhausted: assignment repair already attempted; '
            'the existing supervisor must resolve the assignment or report the '
            'specific external blocker. No new block or repair was recorded.'
        )
    name, required = _supervisor(task)
    if name is None:
        raise AssignmentRoutingError(
            f'no_capable_supervisor: task {task.id} requires a different profile '
            'with its original capabilities and kanban tools; configure '
            'kanban.orchestrator_profile or a capable creator profile.'
        )
    # The enclosing block transaction owns the run CAS and validated this profile.
    # assign_task's post-commit observer cannot run inside this transaction.
    conn.execute(
        'UPDATE tasks SET assignee=?, consecutive_failures=0, last_failure_error=NULL WHERE id=?',
        (name, task.id),
    )
    kb._append_event(conn, task.id, 'assigned', {'assignee': name, 'from': task.assignee})
    supervisor_id = kb.create_task(
        conn, title=f'Repair assignment: {task.title}',
        body=(f'Task {task.id} cannot run under profile {task.assignee!r}. '
              f'Block reason: {payload.get("reason") or "assignment mismatch"}\n'
              f'The original has been reassigned to verified profile {name!r}. '
              'Inspect the original task and verify its assignment and requirements '
              'before completing this repair. Record the verification evidence. '
              'Do not execute, unblock or recreate the original task here; it waits '
              'on this repair dependency and runs separately after verification. '
              'Do not request credentials on behalf of a worker or create another supervisor.'),
        assignee=name, created_by=task.assignee, workspace_kind='scratch',
        requires=required, idempotency_key=key, creator_task_id=task.id,
        session_id=task.session_id, tenant=task.tenant, priority=task.priority,
    )
    kb._link(conn, supervisor_id, task.id)
    conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (task.id,))
    payload['supervisor_task_id'] = supervisor_id
    payload['supervisor_profile'] = name
