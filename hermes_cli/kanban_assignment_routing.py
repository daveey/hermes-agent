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


class ExecutionAuthorityMismatch(AssignmentRoutingError):
    """The block asks for a board transition on another card, which no profile change grants."""

    code = 'execution_authority_mismatch'


# A worker of ANY profile is fenced from moving another card through its lifecycle, so a block
# asking for that is an execution-authority gap, not a profile mismatch: reassigning cannot fix it.
_ID = r'(t_[0-9a-f]{8,16})\b'
# Imperative ask forms only (base verb, also inside kanban_unblock / unblock_task): "unblock t_x",
# "kanban_unblock(t_x)", "re-queue t_x", "move t_x back to ready", "t_x needs an unblock". Past and
# progressive forms ("was unblocked by t_x", "promotion of t_x") describe history, not an ask.
_VERB = r'(?<![A-Za-z])(?:kanban_)?(?:un-?block|re-?queue|promote|reopen|release)(?![A-Za-z])'
_OTHER_CARD_TRANSITION = re.compile(
    _VERB + r'[^\n]{0,80}?' + _ID
    + r'|(?<![A-Za-z])resume\s+(?:card\s+|task\s+)?' + _ID
    + r'|(?<![A-Za-z])move\s+' + _ID + r'[^\n]{0,30}?\bto\s+(?:ready|todo)\b'
    + r'|' + _ID + r'[^\n.;]{0,40}?' + _VERB,
    re.IGNORECASE,
)
_BUDGET_RESET = re.compile(
    r'\b(?:reset|raise|increase|extend|clear|lift)\b[^\n]{0,40}?\bcontinuation[_ ](?:budget|limit|streak|count)\b',
    re.IGNORECASE)


# Model edits require the same non-worker authority as lifecycle edits. A new
# profile cannot grant it. Bound matches to one clause and imperative verb forms.
_MODEL_FIELD = r'\b(?:model|provider|pin|model_override|provider_override)\b'
_EDIT_VERB = r'(?<![A-Za-z])(?:change|set|clear|edit|replace|remove)(?![A-Za-z])'
_CLAUSE = r'[^\n.;]{0,100}?'
_OTHER_CARD_MODEL_EDIT = re.compile(
    _EDIT_VERB + _CLAUSE + _ID + _CLAUSE + _MODEL_FIELD
    + r'|' + _EDIT_VERB + _CLAUSE + _MODEL_FIELD + _CLAUSE + _ID
    + r'|' + _ID + r"(?:'s)?\s+needs?\s+(?:a\s+)?" + _MODEL_FIELD
    + r'\s+(?:change|edit|replacement)\b', re.IGNORECASE,
)


def _model_edit_need(task_id, reason):
    ids = [next(g for g in m if g) for m in _OTHER_CARD_MODEL_EDIT.findall(reason or '')]
    return [i for i in dict.fromkeys(ids) if i != task_id]


def execution_authority_need(task_id, reason):
    """The other card ids a block asks to move through its lifecycle (or ``['<budget>']`` for a
    continuation-budget reset), else ``[]``. Profile mismatches return ``[]``."""
    text = reason or ''
    ids = [next(g for g in m if g) for m in _OTHER_CARD_TRANSITION.findall(text)]
    ids = [i for i in dict.fromkeys([*ids, *_model_edit_need(task_id, text)]) if i != task_id]
    if not ids and _BUDGET_RESET.search(text):
        ids = ['<budget>']
    return ids


def _authority_error(task_id, needs, reason=None):
    if _model_edit_need(task_id, reason):
        return ExecutionAuthorityMismatch(
            f'execution_authority_mismatch: task {task_id} asks to edit another card model/provider '
            f'({", ".join(needs)}). Every worker profile has the same mutation fence; '
            'assignment repair cannot grant non-worker authority and none was recorded. '
            'No supported supervisor model-edit operation exists, and none is needed for a quota '
            'wall: the dispatcher already runs a non-mandatory pin on a same-class peer while its '
            'provider is walled 6 h or more (kanban_pin_reroute, `model_reroute` event). Preserve mandatory model pins '
            'and subscription billing policy; recover availability through the existing owner '
            'or an authorized non-worker operator, not another profile or repeated repair cards. '
            'A legacy repair cannot complete on profile existence alone; archive it before '
            'recovering through the appropriate owner.')
    return ExecutionAuthorityMismatch(
        f'execution_authority_mismatch: task {task_id} asks to move another card '
        f'({", ".join(needs)}) through its lifecycle. That is fenced for every worker profile, so an '
        'assignment repair cannot fix it and none was recorded. For a card the continuation budget '
        'parked, file kanban_supervisor_request ops=[{"op": "resume", "task_id": "<id>"}] (the '
        'non-worker executor re-checks scope and refuses human-gated cards); a card waiting on a '
        'human stays with that human. A repair card already filed for such an ask cannot complete: '
        'archive it (an archived parent no longer gates the original), then file that request.'
    )


def classify(task, kind, reason, *, human_request=None):
    """Use explicit request evidence, never auth vocabulary, to stop assignment repair."""
    # Routing grants no execution authorization. Prose may describe denied or
    # hypothetical authentication routes; only validated actionability evidence
    # makes this a human request rather than supervised operational recovery.
    from hermes_cli.kanban_human_request import validate_human_request
    if validate_human_request(human_request, kind=kind) is not None:
        return ('capability', 'human_access_request') if kind == 'assignment' else (kind, None)
    if kind == 'assignment':
        return kind, None
    if (kind != 'capability' or _HUMAN_ACCESS.search(reason or '')
            or not _WORKER_MISMATCH.fullmatch((reason or '').strip())):
        return kind, None
    try:
        validate_assignment(task.assignee, requires=task.requires,
                            workspace_kind=task.workspace_kind,
                            workspace_path=task.workspace_path, skills=task.skills)
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
            from hermes_cli.config_backend import config_exists
            if not config_exists(Path(resolve_profile_env(name)) / 'config.yaml'):
                continue
            # A profile no pool worker or local dispatcher can run strands the card it is handed (t_f6d930b7).
            from hermes_cli.kanban_profile_runnable import runnable
            if not runnable(name):
                continue
            required = validate_assignment(name, requires=[*(task.requires or []), 'kanban'],
                                           workspace_kind=task.workspace_kind,
                                           workspace_path=task.workspace_path, skills=task.skills)
        except (ValueError, OSError):
            continue
        return name, required
    return None, []


def supervisor_available(task) -> bool:
    """True when kind='assignment' can route *task*: a different, existing, capable profile is configured.

    Same check as :func:`route` (``_supervisor``), no new IO. Guidance texts offer assignment only when this
    holds: on single-profile hosts (no orchestrator_profile, creator == assignee) it is always
    ``no_capable_supervisor``. [daveey]
    """
    return _supervisor(task)[0] is not None


# The routes that work without a second profile. Shown wherever assignment cannot route (the
# agent_owned_block refusal, route()'s no_capable_supervisor refusal). [daveey]
NO_SECOND_PROFILE_ROUTES = (
    "(a) work that needs another gateway's access -> kanban_create the owner card on the gateway that "
    "holds it (placement='<gateway>'), then park on it with kind='time', awaiting={kind:'card', "
    "ref:'<owner id>', gateway:'<gateway>'}; (b) another gateway holds the access -> kanban_block("
    "needs=['host:<gateway>'] or the exact capability, e.g. 'ssh:mini') so an owner card opens there; "
    "(c) archive, unlink, resume or retry_limit on a card in your creator tree -> "
    "kanban_supervisor_request(ops=[{'op': 'archive'|'unlink'|'resume'|'retry_limit', ...}])."
)


def validate_repair_completion(conn, task_id, *, observation=None):
    """A completed repair must not release an invalid assignment after policy drift."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_assignment_original import repair_key, resolve

    key = repair_key(conn, task_id)
    if key is None:
        if observation is not None:
            raise AssignmentRoutingError('Assignment repair key changed during validation; retry')
        return
    original = kb.get_task(conn, key.removeprefix(_SUPERVISOR_PREFIX))
    if observation is not None and original is not None:
        raise AssignmentRoutingError('Assignment repair original custody changed during validation; retry')
    if original is None:
        original, payloads = resolve(conn, task_id, key, observation)
    else:
        payloads = tuple(row[0] for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind IN ('blocked','block_loop_detected') ORDER BY id DESC",
            (original.id,)).fetchall())
    if original.assignee is None:
        raise AssignmentRoutingError('Assignment repair has no assigned original task')
    # A verified profile is not a recovery when the original asked for authority no profile has.
    import json
    for raw in payloads:
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            continue
        if isinstance(data, dict) and data.get('supervisor_task_id') == task_id:
            reason = data.get('reason')
            # A reasonless block (CLI/dashboard default) is a supported producer
            # and asks for no authority; only a non-string reason is malformed.
            if reason is not None and not isinstance(reason, str):
                raise AssignmentRoutingError('Assignment repair has invalid authority history')
            needs = execution_authority_need(original.id, reason)
            if needs:
                raise _authority_error(original.id, needs, reason)
            break
    else:
        # A local row can arrive before its history. Its assignment alone cannot
        # establish that completing this repair is authorized.
        raise AssignmentRoutingError('Assignment repair has no matching authority history')
    validate_assignment(original.assignee, requires=original.requires,
                        workspace_kind=original.workspace_kind,
                        workspace_path=original.workspace_path, skills=original.skills)
    from hermes_cli.kanban_model_assignment import validate_model_assignment
    from hermes_cli.kanban_model_neutral import routing_view
    route = routing_view(original)
    validate_model_assignment(original.assignee, route.model_override, route.provider_override)


def route(conn, task, payload):
    """Create at most one repair card per original, atomically with its wait edge."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_model_neutral import forwarded_text, forwarded_title

    key = _SUPERVISOR_PREFIX + task.id
    needs = execution_authority_need(task.id, payload.get('reason'))
    if needs:
        raise _authority_error(task.id, needs, payload.get('reason'))
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
            'with its original capabilities and kanban tools. No different profile is '
            'configured on this host, so assignment cannot help; use one of: '
            + NO_SECOND_PROFILE_ROUTES
        )
    # The enclosing block transaction owns the run CAS and validated this profile.
    # assign_task's post-commit observer cannot run inside this transaction.
    conn.execute(
        'UPDATE tasks SET assignee=?, consecutive_failures=0, last_failure_error=NULL WHERE id=?',
        (name, task.id),
    )
    kb._append_event(conn, task.id, 'assigned', {'assignee': name, 'from': task.assignee})
    supervisor_id = kb.create_task(
        conn, title=forwarded_title(f'Repair assignment: {task.title}', f'Repair assignment: {task.id}'),
        body=(f'Task {task.id} cannot run under profile {task.assignee!r}. '
              f'Source title (historical evidence):\n{forwarded_text(task.title)}\n'
              f'Block reason (historical evidence):\n{forwarded_text(payload.get("reason") or "assignment mismatch")}\n'
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
    payload['status'] = 'todo'  # where the card landed (block_task recorded 'blocked'); the sync heal restores it
    payload['supervisor_task_id'] = supervisor_id
    payload['supervisor_profile'] = name
