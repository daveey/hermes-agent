"""Local original-source custody; metadata grants no execution authority. [daveey]"""
from __future__ import annotations

import json

from hermes_cli import kanban_db_sync as sync


def materialize(conn, hub, board):
    """Defer canonical work until the successful home sync round, outside writers."""
    from hermes_cli.kanban_actionability_materialize import run
    run(conn, hub, board)


def ensure_schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS kanban_actionability_intents ('
                 'event_uid TEXT PRIMARY KEY, task_id TEXT NOT NULL, source_json TEXT NOT NULL, '
                 'source_kind TEXT NOT NULL, source_at INTEGER NOT NULL, deadline_at INTEGER, '
                 "state TEXT NOT NULL DEFAULT 'pending', reason TEXT, op_id TEXT, last_checked_at INTEGER NOT NULL DEFAULT 0)")


def capture(conn, event_uid):
    """Insert only new stamped home events, atomically with append/import; no network."""
    if sync.board_mode(conn) != 'primary' or not event_uid:
        return
    state = sync.sync_state(conn) or {}
    source = conn.execute('SELECT e.*, t.created_on, t.sync_version FROM task_events e JOIN tasks t ON t.id=e.task_id WHERE e.uid=?',
                          (event_uid,)).fetchone()
    if source is None:
        return
    gateway = state.get('gateway_id')
    local_insert = (source['created_on'] is None and source['sync_version'] in (None, 0)
                    and not state.get('applying') and isinstance(gateway, str) and bool(gateway.strip())
                    and conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='task_events' AND row_uid=? "
                                     "AND op='upsert' AND state='pending'", (event_uid,)).fetchone() is not None)
    # Local pre-ACK custody is not proof of home: canonical observation still owns that check.
    if not local_insert and (not source['created_on'] or source['created_on'] != gateway):
        return
    try:
        data = json.loads(source['payload'] or '{}')
    except (ValueError, TypeError):
        return
    intent = data.get('actionability_intent') if isinstance(data, dict) else None
    if not isinstance(intent, dict) or type(intent.get('version')) is not int or intent['version'] != 1:
        return
    from hermes_cli.kanban_actionability_creation import intent_for
    recorded_owner = intent.get('owner')
    refs = recorded_owner.get('refs') if isinstance(recorded_owner, dict) else None
    assignee = refs[0] if isinstance(refs, list) and len(refs) == 1 else None
    expected = intent_for({'id': source['task_id'], 'status': intent.get('status'), 'assignee': assignee}, source['kind'],
                          data, now=source['created_at'])
    valid = expected is not None and json.dumps(expected, sort_keys=True) == json.dumps(intent, sort_keys=True)
    reason = expected.get('invalid_reason') if valid else 'creation_intent_invalid'
    deadline = expected['deadline_at'] if valid and not reason else None
    if not conn.in_transaction:
        raise ValueError('source intent capture requires source write transaction')
    ensure_schema(conn)
    conn.execute('INSERT OR IGNORE INTO kanban_actionability_intents '
                 '(event_uid,task_id,source_json,source_kind,source_at,deadline_at,state,reason) VALUES (?,?,?,?,?,?,?,?)',
                 (event_uid, source['task_id'], source['payload'], source['kind'], source['created_at'],
                  deadline, 'invalid' if reason else 'pending', reason))
