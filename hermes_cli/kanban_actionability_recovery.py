"""Explicit frozen administrative obligations; no execution/retry authority. [daveey]"""
from __future__ import annotations

import json

from hermes_cli import kanban_actionability_observation as observation
from hermes_cli import kanban_actionability_transport as transport
from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_db_connect import write_txn

ACTIONS = {'execute_current_scope', 'review_current_scope', 'satisfy_dependency', 'restore_capability',
           'decide_scope', 'resolve_release_authority', 'supply_recorded_human_step',
           'wait_for_named_producer', 'repair_lifecycle_contract'}


def _schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS kanban_actionability_previews ('
                 'op_id TEXT PRIMARY KEY, board TEXT NOT NULL, home_gateway TEXT NOT NULL, '
                 'request_json TEXT NOT NULL, request_sha256 TEXT NOT NULL, state TEXT NOT NULL)')


def _home(conn, hub, board):
    if conn.in_transaction or sync.board_mode(conn) != 'primary':
        raise ValueError('recovery requires primary home connection outside writer transaction')
    state = sync.sync_state(conn) or {}
    if not isinstance(board, str) or not board or state.get('board_id') != board:
        raise ValueError('recovery board mismatch with local custody')
    if state.get('gateway_id') != hub.ident.gateway:
        raise ValueError('recovery authenticated home differs from local custody')


def _text(value, maximum):
    # JS string length counts UTF-16 code units; reject lone surrogates too.
    if (not isinstance(value, str) or not value.strip('\ufeff \t\r\n\v\f\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000')
            or len(value.encode('utf-16-le')) // 2 > maximum):
        raise ValueError('invalid recovery text or UTF-16 length')


def _owner(source, owner):
    if (not isinstance(owner, dict) or set(owner) != {'kind', 'refs'}
            or not isinstance(owner['refs'], list) or len(owner['refs']) != 1):
        raise ValueError('supported accountable recovery owner required')
    _text(owner['refs'][0], 512)
    facts = source['facts']
    if owner['kind'] == 'assignee':
        valid = owner['refs'] == [facts['assignee']]
    else:
        valid = owner['kind'] == 'task' and owner['refs'][0] in source['owner_task_refs']
    if not valid:
        raise ValueError('owner unavailable: require exact assignee or canonically linked open parent')


def _payload(source, *, owner, action, instruction, prerequisite, trigger, evidence, deadline_at, now):
    _owner(source, owner)
    facts = source['facts']
    if action not in ACTIONS:
        raise ValueError('unsupported recovery action')
    for text in (instruction, prerequisite, trigger):
        _text(text, 1000)
    if not isinstance(evidence, list) or len(evidence) > 8:
        raise ValueError('invalid recovery evidence')
    for text in evidence:
        _text(text, 512)
    if (type(deadline_at) is not int or not source['first_observed'] < deadline_at <= now + 14 * 86400
            or source['first_observed'] > now):
        raise ValueError('explicit fixed deadline must follow source clock and be within canonical 14-day bound')
    return {'version': 1, 'facts': facts, 'owner': owner, 'action': action, 'instruction': instruction,
            'blocker': {'prerequisite': prerequisite, 'trigger': trigger, 'evidence': evidence},
            'first_observed': source['first_observed'], 'deadline_at': deadline_at, 'legacy': False}


def preview(conn, hub, board, task_id, **obligation):
    """Freeze explicit operator facts without outbox issuance. Never invent a deadline."""
    _home(conn, hub, board)
    cap = transport.capability(hub, board)
    source = observation.observe(hub, board, task_id)
    payload = _payload(source, **obligation, now=int(cap['hlc'][:13]) // 1000)
    wire = transport.envelope(task_id, payload)
    raw = transport.canonical(wire)
    sha = transport.digest(raw)
    with write_txn(conn):
        _schema(conn)
        old = conn.execute('SELECT * FROM kanban_actionability_previews WHERE op_id=?',
                           (wire['client_op_id'],)).fetchone()
        if old:
            if old['request_json'] != raw or old['board'] != board or old['home_gateway'] != hub.ident.gateway:
                raise ValueError('recovery preview already frozen with different bytes')
            if old['state'] != 'previewed':
                raise ValueError('recovery preview already reserved or stale; use original digest to replay')
        else:
            conn.execute('INSERT INTO kanban_actionability_previews VALUES (?,?,?,?,?,?)',
                         (wire['client_op_id'], board, hub.ident.gateway, raw, sha, 'previewed'))
    return {'op_id': wire['client_op_id'], 'request_sha256': sha, 'request': wire,
            'state': 'previewed', 'execution_authority': False, 'issued': False}


def _frozen(conn, hub, board, op_id, sha):
    _home(conn, hub, board)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_actionability_previews'").fetchone():
        raise ValueError('recovery preview unavailable')
    row = conn.execute('SELECT * FROM kanban_actionability_previews WHERE op_id=?', (op_id,)).fetchone()
    if (not row or row['board'] != board or row['home_gateway'] != hub.ident.gateway
            or row['request_sha256'] != sha or transport.digest(row['request_json']) != sha):
        raise ValueError('recovery frozen digest or home mismatch')
    wire = json.loads(row['request_json'])
    if transport.envelope(wire['task'], wire['payload']) != wire or wire['client_op_id'] != op_id:
        raise ValueError('recovery frozen identity mismatch')
    return dict(row), wire


def apply(conn, hub, board, op_id, sha):
    """Digest confirmation: preflight only before issuance; uncertain sends replay exactly."""
    row, wire = _frozen(conn, hub, board, op_id, sha)
    if row['state'] == 'previewed':
        source = observation.observe(hub, board, wire['task'])
        p = wire['payload']
        if (source['home_gateway'] != row['home_gateway'] or source['facts'] != p['facts']
                or source['first_observed'] != p['first_observed']):
            with write_txn(conn):
                conn.execute("UPDATE kanban_actionability_previews SET state='stale' WHERE op_id=? AND state='previewed'",
                             (op_id,))
            raise ValueError('recovery source changed; frozen preview is stale, no request issued')
        _owner(source, p['owner'])
        transport.capability(hub, board)
        with write_txn(conn):
            current = conn.execute('SELECT * FROM kanban_actionability_previews WHERE op_id=?', (op_id,)).fetchone()
            if dict(current) != row:
                raise ValueError('recovery preview changed during preflight')
            transport.reserve(conn, wire['task'], p)
            conn.execute("UPDATE kanban_actionability_previews SET state='reserved' WHERE op_id=?", (op_id,))
    elif row['state'] != 'reserved':
        raise ValueError('recovery preview stale; no request issued')
    return transport.confirm(conn, hub, board, op_id)


def escalate(conn, hub, board, op_id, sha):
    """Explicit overdue reconsideration, only after independently confirmed root custody."""
    row, wire = _frozen(conn, hub, board, op_id, sha)
    root = transport.command(conn, op_id)
    if (row['state'] != 'reserved' or not root or root['delivery_state'] != 'confirmed'
            or root['request_json'] != row['request_json'] or root['request_sha256'] != sha):
        raise ValueError('recovery root custody must be confirmed before escalation')
    cap = transport.capability(hub, board)
    escalation = transport.envelope(wire['task'], wire['payload'], 'escalation')
    transport.escalation_ready(hub, board, escalation, cap)
    with write_txn(conn):
        owned = transport.reserve(conn, wire['task'], wire['payload'], phase='escalation')
    return transport.confirm(conn, hub, board, owned['op_id'])
