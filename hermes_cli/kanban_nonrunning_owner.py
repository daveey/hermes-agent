"""Optional diagnostics at the authenticated owner-ledger acquisition boundary.

A matching hash is integrity, not authentication. Call only after the existing
ledger's exact authenticated owner-record/version/scope freshness checks.
"""
from __future__ import annotations


def fields(task, owner, *, current, now):
    from hermes_cli import kanban_origin_ledger as ledger
    from hermes_cli.kanban_execution_observations import card_raw
    from hermes_cli.kanban_nonrunning_reason import select
    if (not current or task.get('status') != 'ready' or task.get('current_run_id') is not None
            or any(task.get(k) != owner.get(k) for k in ('gateway', 'profile', 'board'))
            or task.get('id') != owner.get('task_id') or not ledger._owner_evidence(task)):
        return {}
    reason = select(task.get('execution_observations'), task, now=now)
    if reason is None:
        return {}
    # Do not carry owner's phase/progress/receipts as a side effect of diagnosis.
    row = {**owner, 'id': owner['task_id'], 'status': 'ready', 'remote': True}
    raw = card_raw(row)
    raw['non_running_reason'] = reason
    return {'execution_observations': [raw]}
