"""Bind opt-in reassessment to its exact captured park op, never latest at send."""
from __future__ import annotations

import json

from hermes_cli.kanban_owner_reassessment import PENDING_TABLE, ReassessmentRefused


def capture(conn, receipt):
    """Bind while the park transaction and its single status capture are live."""
    task = receipt['task_id']
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_sync_outbox'").fetchone():
        return
    changed = conn.execute('SELECT status,n FROM temp.kanban_status_changes WHERE task_id=?', (task,)).fetchone()
    rows = conn.execute('SELECT op_id FROM temp.kanban_status_capture WHERE task_id=?', (task,)).fetchall()
    if changed is None or tuple(changed) != ('scheduled', 1) or len(rows) != 1:
        raise ReassessmentRefused('reassessment needs one exact status capture')
    op_id = rows[0][0]
    from hermes_cli.kanban_sync_status_origin import current_origin
    ordinary_origin = current_origin(conn, task)
    if ordinary_origin is None:
        raise ReassessmentRefused('reassessment ordinary capture origin unavailable')
    original = conn.execute('SELECT fields,wire_json FROM kanban_sync_outbox WHERE op_id=?', (op_id,)).fetchone()
    if original is None or original[1] is not None:
        raise ReassessmentRefused('reassessment ordinary capture already frozen')
    fields = json.loads(original[0])
    fields['owner_reassessment_fallback_status_origin'] = ordinary_origin
    conn.execute('UPDATE kanban_sync_outbox SET fields=? WHERE op_id=? AND wire_json IS NULL',
                 (json.dumps(fields, ensure_ascii=False, separators=(',', ':')), op_id))
    conn.execute('UPDATE kanban_sync_outbox SET status_origin=? WHERE op_id=? AND wire_json IS NULL',
                 (receipt['park_event']['uid'], op_id))
    bind(conn, op_id=op_id, task_id=task, event_uid=receipt['park_event']['uid'])


def captured_origin(conn, op_id, fallback):
    """Read only this op's transaction-captured binding; never search latest task."""
    row = conn.execute('SELECT fields,status_origin FROM kanban_sync_outbox WHERE op_id=?', (op_id,)).fetchone()
    if row is None:
        return fallback
    fields = json.loads(row[0])
    receipt = fields.get('owner_reassessment_v1')
    if receipt is None:
        return fallback
    saved = conn.execute(f'SELECT receipt FROM {PENDING_TABLE} WHERE event_uid=? AND task_id=?',
                         (row[1], receipt['task_id'])).fetchone()
    if saved is None or json.loads(saved[0]) != receipt or receipt['park_event']['uid'] != row[1]:
        raise ReassessmentRefused('reassessment exact capture changed')
    return row[1]


def bind(conn, *, op_id, task_id, event_uid):
    """Called only for a transaction's single unambiguous status transition.

    An absent journal is the legacy case. A recorded receipt cannot be silently
    attached to a synthesized parent, another transition, or a frozen envelope.
    Board still requires the matching live lease at actual delivery.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (PENDING_TABLE,)).fetchone():
        return
    saved = conn.execute(f'SELECT receipt FROM {PENDING_TABLE} WHERE task_id=? AND event_uid=?',
                         (task_id, event_uid)).fetchone()
    if saved is None:
        return
    receipt = json.loads(saved[0])
    row = conn.execute('SELECT fields,wire_json,status_origin FROM kanban_sync_outbox '
                       "WHERE op_id=? AND row_uid=? AND table_name='tasks' AND op='upsert'",
                       (op_id, task_id)).fetchone()
    if row is None or row[1] is not None or row[2] != event_uid:
        raise ReassessmentRefused('reassessment capture is not the exact unfrozen park op')
    fields = json.loads(row[0])
    if fields.get('status') != 'scheduled' or receipt['park_event']['uid'] != event_uid:
        raise ReassessmentRefused('reassessment capture does not carry the park transition')
    if 'owner_reassessment_v1' in fields and fields['owner_reassessment_v1'] != receipt:
        raise ReassessmentRefused('reassessment capture identity changed')
    fields['owner_reassessment_v1'] = receipt
    conn.execute('UPDATE kanban_sync_outbox SET fields=? WHERE op_id=? AND wire_json IS NULL',
                 (json.dumps(fields, ensure_ascii=False, separators=(',', ':')), op_id))
