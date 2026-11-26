"""Read-only stop recovery diagnostics; these never grant lifecycle authority. [daveey]"""
from __future__ import annotations

from hermes_cli import kanban_spend_decision as decision


def describe(conn, task_id: str) -> dict:
    """Describe effective restrictions before suggesting an operation, independently of caller identity.

    A capability stop is not a scope stop, but an older uncovered restriction still dominates it.
    Keep this advisory separate from authorization and every mutation precondition.
    """
    from hermes_cli import kanban_scope_renewal as renewal
    from hermes_cli.kanban_containment_connections import held as contained
    from hermes_cli.kanban_sync_quarantine import refusal as quarantined
    from hermes_cli.kanban_refusal_receipt import restricted as safety_restricted
    from hermes_cli.kanban_stop_ended import stopped_at
    from hermes_cli.kanban_db import _json_dict

    stop = decision.pending_episode(conn, task_id)
    payload = _json_dict(stop['payload']) if stop else {}
    cause = payload.get('cause')
    evidence = ({'episode': renewal.key(stop), 'kind': stop['kind'], 'cause': cause,
                 'accounting_boundary': payload.get('accounting_boundary')} if stop else None)
    if safety_restricted(conn, task_id):
        operation, instruction = 'resolve_safety_boundary', (
            'Provider safety restriction retained; no supported execution release. '
            'Owning supervisor must resolve the scope/authority boundary without rerunning refused work.')
    elif quarantine := quarantined(conn, task_id):
        operation, instruction = 'resolve_sync_quarantine', quarantine
    elif contained(conn, task_id):
        operation, instruction = 'resume-contained', 'Use the exact contained episode and scoped resume authority.'
    elif stopped_at(conn, task_id) is not None:
        operation, instruction = 'resolve_administrative_stop', (
            'Owning supervisor must resolve the recorded administrative stop; scope renewal does not cover it.')
    elif decision.execution_restricted(conn, task_id):
        operation, instruction = 'renew-scope', renewal.ROUTE.format(task=task_id)
    elif stop and (cause == 'capability_unavailable' or cause == 'scope_stopped'):
        # A covered scope stop can still await its separate guarded promotion.
        operation, instruction = 'promote', (
            f"Read the current card and record the whole-acceptance mandate, then preflight on its home gateway: "
            f"`hermes kanban promote {task_id} '<what changed; preserve original acceptance>' "
            '--dry-run --json`. Honor any exact --seen requirement and every independent guard; '
            'recheck the existing spend window before changing any budget.')
    else:
        operation, instruction = 'inspect_stop', 'Read the exact current stop and restrictions before choosing recovery.'
    return {'version': 1, 'stop': evidence, 'next_operation': operation, 'instruction': instruction,
            'expected_actor': ['owning_chat', 'authorized_operator'], 'authorization': 'not_granted'}


def refusal_context(conn, task_id: str) -> tuple[dict, str]:
    """Structured and rendered advice for a refused release; no writes, grants or identity assertions."""
    recovery = describe(conn, task_id)
    stop = recovery['stop']
    cause = str(stop['cause']) if stop else 'no pending stop'
    text = (f" Current stop: {cause}; next guarded candidate: {recovery['next_operation']}. "
            + recovery['instruction']
            + ' Expected actor: the owning chat or an authorized operator, subject to that operation\'s guards. '
            'run_in_user_terminal remains agent-origin; opening a terminal or confirming its command is not '
            'human authentication. Use the original owning chat on its authenticated gateway, not an SSH '
            'session identity assertion. This diagnostic grants no authority.')
    return recovery, text
