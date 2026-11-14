"""Exact remote notice custody from canonical successful automation settlement.

Update resume deliberately strips message control metadata. A completed admission and its
atomically committed execution result remain evidence even without an original keyed row.
This reads the private admitted envelope, never arbitrary message/display metadata.
"""
import json
import sqlite3

from hermes_state_terminal import RESULT_PREFIX
from tui_gateway.kanban_remote_fifo import lineage_ids


def completed_notice(db, session_id, key):
    """A persisted successful Kanban wake in this lineage consumed this exact event."""
    if not key or not hasattr(db, '_read_ctx'):
        return False
    ids = sorted(lineage_ids(db, session_id))
    marks = ','.join('?' for _ in ids)
    try:
        with db._read_ctx() as conn:
            rows = conn.execute(
                'SELECT a.principal_id,a.request_id,a.target_session_id,a.payload_json,s.value '
                'FROM session_admissions a JOIN state_meta s ON s.key=? || a.admission_id '
                f'WHERE (a.target_session_id IN ({marks}) OR '
                f"json_extract(a.payload_json, '$.local_automation_v1.target') IN ({marks})) "
                "AND a.status='terminal' AND a.outcome='completed' "
                "AND EXISTS (SELECT 1 FROM json_each(a.payload_json, "
                "'$.local_automation_v1.kanban_event_keys') WHERE value=?)",
                (RESULT_PREFIX, *ids, *ids, key))
            return any(_successful_wake(row) for row in rows)
    except (sqlite3.Error, ValueError, TypeError):
        # Missing/unreadable proof is not permission to acknowledge an undelivered event.
        return False


def _successful_wake(row):
    principal, request, target, payload_json, result_json = row
    descriptor = json.loads(payload_json)['local_automation_v1']
    identity = json.loads(request)
    stored = json.loads(result_json)
    result = stored.get('result') if isinstance(stored, dict) else None
    return (isinstance(identity, list) and identity[:1] == ['kanban-wake']
            and principal == 'automation:' + str(descriptor.get('route'))
            and descriptor.get('identity') == request and descriptor.get('owner') == target
            and descriptor.get('display_kind') == 'kanban_notice'
            and isinstance(result, dict) and result.get('completed') is True
            and result.get('agent_persisted') is True
            and not any(result.get(flag) for flag in ('failed', 'error', 'interrupted', 'paused_for_update')))
