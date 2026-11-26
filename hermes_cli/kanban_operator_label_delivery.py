"""Epoch-bound result delivery for label jobs; original wire survives retry. [daveey]"""
from __future__ import annotations

import json
import time
import uuid

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_home_command as home, kanban_home_memo as memo
from hermes_cli import kanban_sync_client as client


def _legacy(conn, event_uid, task):
    """Record producer-version custody BEFORE identity/snapshot I/O can fail.

    Attempts are retry scheduling, not proof that bytes were transmitted. Only
    pre-upgrade attempted jobs without this durable discriminator stay unknown.
    """
    with kb.write_txn(conn):
        from hermes_cli.kanban_home_transfer import frozen
        if frozen(conn, task):
            raise ValueError('home transfer pending')
        conn.execute('CREATE TABLE IF NOT EXISTS kanban_home_label_capture1 '
                     '(event_uid TEXT PRIMARY KEY, task_id TEXT NOT NULL, legacy INTEGER NOT NULL)')
        row = conn.execute('SELECT task_id,legacy FROM kanban_home_label_capture1 WHERE event_uid=?', (event_uid,)).fetchone()
        if row is not None:
            if row[0] != task:
                raise ValueError('label capture identity changed')
            return bool(row[1])
        job = conn.execute('SELECT delivery_attempts FROM operator_label_jobs WHERE event_uid=?', (event_uid,)).fetchone()
        legacy = job is None or job[0] > 0
        conn.execute('INSERT INTO kanban_home_label_capture1 VALUES (?,?,?)', (event_uid, task, int(legacy)))
        return legacy


def push_event(conn, board, own, task, event_uid, payload):
    from hermes_cli.kanban_operator_call import RESULT_KIND

    legacy = _legacy(conn, event_uid, task)
    hub = client.Hub(client.load_identity())
    try:
        encoded = json.dumps(payload, sort_keys=True)
        op = memo.get(conn, event_uid)
        if op is None:

            # Existing attempted jobs have UNKNOWN capture custody. Never enrich
            # them from today's epoch. They remain fail-closed on a moved task.
            evidence = hub.row_evidence(board, 'tasks', task)
            if evidence.get('state') == 'deleted':
                return 'rejected', 'not_found: canonical label result target deleted'
            target = evidence.get('row') if evidence.get('state') == 'live' else None
            captured = home.from_evidence(task, target)
            epoch = None if legacy else captured
            with kb.write_txn(conn):
                raw = {'client_op_id': uuid.uuid4().hex, 'op': 'upsert', 'table': 'task_events', 'uid': event_uid,
                       'hlc': client._next_hlc(conn, own), 'fields': {'task_id': task, 'kind': RESULT_KIND,
                       'payload': encoded, 'created_at': int(time.time())}}
                op = memo.reserve(conn, event_uid, task, raw, epoch=epoch, foreign=True)
        if op.get('uid') != event_uid or op.get('fields', {}).get('task_id') != task or op['fields'].get('payload') != encoded:
            raise ValueError('label result delivery identity changed')
        res = (hub.ops(board, [op]).get('results') or [{}])[0]
        if not isinstance(res, dict) or res.get('client_op_id') != op['client_op_id']:
            raise ValueError('label result delivery response identity unconfirmed')
        status = str(res.get('status') or '')
        if status in ('applied', 'merged'):
            proof = hub.row_evidence(board, 'task_events', event_uid)
            row = proof.get('row') if proof.get('state') == 'live' else None
            if (not isinstance(row, dict) or row.get('uid') != event_uid or row.get('origin_gateway') != own
                    or any(row.get(k) != v for k,v in op['fields'].items())):
                raise ValueError('label result canonical readback unconfirmed')
        with kb.write_txn(conn):
            memo.settled(conn, event_uid, status)
        return status, f"{res.get('reason') or ''}: {res.get('detail') or ''}".strip(': ')
    finally:
        hub.close()
