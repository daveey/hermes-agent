"""Retire only an upload the live-claim fence already removed locally. [daveey]"""
from __future__ import annotations

import json

from hermes_cli import kanban_db_sync as sync


def snapshot(conn, op, task):
    """Include provenance and mode in the resolver's compare-and-delete snapshot."""
    if op['table_name'] != 'task_events' or not task:
        return None
    audits = []
    for row in conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='sync_rejected'", (task,)):
        try:
            payload = json.loads(row['payload'] or '')
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get('op_id') == op['op_id']:
            audits.append(dict(row))
    return {'mode': (sync.sync_state(conn) or {}).get('mode'), 'audits': audits}


def moot_event(op, before, evidence):
    """Never replay a phantom lifecycle event or remove its anti-resurrection tombstone.

    This does not authorize a lifecycle write: another worker may still hold the lease.
    The immutable rejection audit and exact local tombstone survive outbox retirement.
    """
    stamp = before.get('fenced_event') or {}
    audits = stamp.get('audits') or []
    tombs = before['tombstones']
    if (op['table_name'] != 'task_events' or op['op'] not in ('upsert', 'compat')
            or str(op.get('last_error') or '').partition(':')[0] != 'live_claim'
            or stamp.get('mode') != 'primary' or before['row'] is not None
            or not before['parent'] or before['parent_ops'] or before['pending'] or before['receipt']
            or len(tombs) != 1 or tuple(tombs[0][:2]) != ('task_events', op['row_uid'])
            or len(audits) != 1):
        return False
    audit = json.loads(audits[0]['payload'])
    fields = json.loads(op['fields'])
    return bool(
        audit.get('table') == 'task_events' and audit.get('uid') == op['row_uid']
        and audit.get('reason') == op['last_error'] and audit.get('fields') == sorted(fields)
        and fields.get('task_id') == before['task']
        and evidence.get('state') == 'never_existed' and evidence.get('floor') == 0
        and evidence.get('row') is None and evidence.get('tombstone') is None)
