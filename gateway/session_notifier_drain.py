"""Selective queued notifier admission during a core-update claim hold.

Receipts stay queued; only their execution waits. Human inputs and other producers
retain their normal admission semantics. Selection is repeated inside the store's
claim transaction so async route validation cannot authorize a different input.
"""
import json


def notifier_wake(row):
    payload = row['payload']
    descriptor = payload.get('local_automation_v1') or (payload.get('native_text_v1') or {}).get('automation') or {}
    identity = descriptor.get('identity')
    if identity != row['request_id'] or not row['principal_id'].startswith('automation:'):
        return False
    try:
        parts = json.loads(identity)
    except (TypeError, ValueError):
        return False
    return (isinstance(parts, list) and len(parts) == 3 and parts[0] == 'kanban-wake'
            and row['principal_id'] == 'automation:' + str(parts[1]))


def held():
    from agent.core_update_drain import active_hold, hold_paths
    from hermes_cli.admission_fence import is_held
    return active_hold() is not None or any(is_held(path.parent) for path in hold_paths())


def eligible(row):
    return not notifier_wake(row) or not held()


def queued_head(pending):
    return next((row for row in pending if row['status'] == 'queued' and eligible(row)), None)
