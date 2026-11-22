"""Read-only separation of rejected retain audit from replica drift. [daveey]

No transport retirement, retention confirmation, or custody/fence change occurs.
The original run must be closed on both copies; ordinary row diff still gates clean.
"""
from __future__ import annotations

import hashlib
import sqlite3


def rejected_summary(live: sqlite3.Connection, shadow: sqlite3.Connection) -> tuple[int, list[str]]:
    """Count every rejection and identify only independently custody-matched retain audits."""
    rows = [dict(r) for r in live.execute("SELECT * FROM kanban_sync_outbox WHERE state = 'rejected'")]
    return len(rows), [r['op_id'] for r in rows if audited_rejection(live, shadow, r)]


def audited_rejection(live: sqlite3.Connection, shadow: sqlite3.Connection, op: dict) -> bool:
    """Fail closed unless immutable custody attests this definitive rejection."""
    from hermes_cli.kanban_retain_transport import canonical, command, frozen_wire

    if (op.get('op') != 'retain' or op.get('table_name') != 'tasks'
            or op.get('state') != 'rejected' or op.get('last_error') != 'missing_decision_episode'):
        return False
    try:
        wire = frozen_wire(op)
        owned = command(live, wire.get('run_uid'))
        text = canonical(wire)
        if (not owned or owned.get('operation_kind') != 'retain'
                or owned.get('delivery_state') != 'rejected' or owned.get('canonical_receipt_json') is not None
                or owned.get('op_id') != op['op_id'] or owned.get('request_uid') != op['op_id']
                or owned.get('task_id') != op['row_uid'] or owned.get('event_uid') != wire.get('event_uid')
                or not owned.get('event_uid') or owned.get('request_json') != text
                or owned.get('request_sha256') != hashlib.sha256(text.encode('utf-8')).hexdigest()):
            return False
        for conn in (live, shadow):
            run = conn.execute('SELECT id,task_id,ended_at FROM task_runs WHERE uid=?',
                               (wire['run_uid'],)).fetchone()
            task = conn.execute('SELECT current_run_id FROM tasks WHERE id=?', (op['row_uid'],)).fetchone()
            if (run is None or run['task_id'] != op['row_uid'] or run['ended_at'] is None
                    or task is None or task['current_run_id'] == run['id']):
                return False
        return True
    except (ValueError, TypeError, KeyError, sqlite3.Error):
        # Legacy schema, malformed custody, or unavailable proof is not settlement.
        return False
