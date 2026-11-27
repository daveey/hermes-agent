"""Frozen final HOME operation and strict canonical application certification.

The private response is historical evidence, not a current task snapshot. Nothing
here applies its task postimage or enables an executor from consumed-only proof.
"""
from __future__ import annotations

import contextvars
import re
import sqlite3
from itertools import pairwise

from hermes_cli import kanban_release_application as app, kanban_release_certificate as cert
from hermes_cli import kanban_release_home_queue as queue, kanban_release_prepared as prepared
from hermes_cli.kanban_containment_functions import register as register_function

MAX_BYTES = 8 * 1024 * 1024
TABLE = 'owner_release_final_wires'
_PERMIT = contextvars.ContextVar('owner_release_final_wire', default=None)


def install(conn):
    queue.install(conn)
    if 'application_digest' not in {r[1] for r in conn.execute(f'PRAGMA table_info({queue.TABLE})')}:
        conn.execute(f'ALTER TABLE {queue.TABLE} ADD COLUMN application_digest TEXT')
    abort = "BEGIN SELECT RAISE(ABORT,'final application custody immutable'); END"
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS owner_release_application_anchor BEFORE UPDATE ON {queue.TABLE} '
                 'WHEN OLD.application_digest IS NOT NULL AND NEW.application_digest IS NOT OLD.application_digest ' + abort)
    register_function(conn, 'kanban_owner_release_final_write', 0,
                      lambda: int(_PERMIT.get() is conn and conn.in_transaction))
    conn.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} (intent_uid TEXT PRIMARY KEY, delivery_uid TEXT NOT NULL UNIQUE, '
                 'operation_uid TEXT NOT NULL UNIQUE, wire TEXT NOT NULL, digest TEXT NOT NULL)')
    for op in ('UPDATE', 'DELETE'):
        conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_{op.lower()} BEFORE {op} ON {TABLE} {abort}')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_insert BEFORE INSERT ON {TABLE} '
                 f'WHEN NOT kanban_owner_release_final_write() {abort}')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_replace BEFORE INSERT ON {TABLE} '
                 f'WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE intent_uid=NEW.intent_uid OR delivery_uid=NEW.delivery_uid '
                 f'OR operation_uid=NEW.operation_uid OR rowid=NEW.rowid) {abort}')


def _proposal_shape(proposal):
    body = proposal['consumption']['intent']
    events, comments = proposal['events'], proposal['comments']
    kinds = ([events[0]['kind']] if events[0]['kind'] == 'wake_episode_evidence' else [])
    kinds += (['commented', 'spend_budget_set'] if comments else [])
    kinds += ['stop_released'] * (len(body['capability_episodes']) + 1) + ['promoted_manual', 'owner_release_applied']
    if ([e.get('kind') for e in events] != kinds
            or any(e.get('origin_gateway') is not None or e.get('run_id') is not None for e in events)
            or any(c.get('origin_gateway') not in (None, proposal['consumption']['renewal']['origin_gateway'])
                   or c.get('run_id') is not None for c in comments)
            or proposal['application']['result']['outcome'] != 'released'
            or [cert.parse(e['payload'])['episode'] for e in events if e['kind'] == 'stop_released']
            != [*body['capability_episodes'], body['episode']]):
        raise ValueError('final application proposal is not the exact canonical sequence')
    if comments:
        comment = comments[0]
        ce = cert.parse(next(e['payload'] for e in events if e['kind'] == 'commented'))
        if (not isinstance(comment.get('body'), str) or not comment['body'].strip() or len(comment['body']) > 16000
                or ce.get('comment_uid') != comment['uid'] or type(ce.get('len')) is not int or ce['len'] != len(comment['body'])):
            raise ValueError('final application proposal portable comment differs')


def _wire(op):
    try:
        return _validated_wire(op)
    except (KeyError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError('invalid final application wire structure') from exc


def _validated_wire(op):
    if (not cert._exact(op, {'client_op_id', 'op', 'task', 'intent_uid', 'proposal'})
            or op['op'] != 'apply_owner_release' or not cert.intent._coordinate(op['client_op_id'])):
        raise ValueError('invalid final application wire')
    proposal = prepared._validate(op['proposal'])
    _proposal_shape(proposal)
    cert.parse(cert.encode(proposal))  # source cap remains 2 MiB
    body = proposal['consumption']['intent']
    reserved = {body['intent_uid'], proposal['delivery_uid'], proposal['consumption']['consume']['client_op_id'],
                *[e['uid'] for e in proposal['events']], *[c['uid'] for c in proposal['comments']]}
    if op['task'] != body['task_id'] or op['intent_uid'] != body['intent_uid'] or op['client_op_id'] in reserved:
        raise ValueError('final application wire identities collide')
    return op


def saved(conn, intent_uid):
    """Exact saved bytes before mutable owner/comments/TTL; missing halves refuse."""
    row = conn.execute(f'SELECT delivery_uid,operation_uid,wire,digest FROM {TABLE} WHERE intent_uid=? '
                       'AND typeof(wire)=\'text\' AND length(CAST(wire AS BLOB))<=?', (intent_uid, MAX_BYTES)).fetchone()
    anchors = conn.execute(f'SELECT delivery_uid,application_digest FROM {queue.TABLE} WHERE intent_uid=? '
                           'AND application_digest IS NOT NULL', (intent_uid,)).fetchall()
    if row is None:
        if anchors:
            raise ValueError('final application wire missing; never remint')
        return None
    op = _wire(cert.parse(row[2], max_bytes=MAX_BYTES))
    proposal = prepared.verified_prepared(conn, intent_uid)
    if (cert.encode(op) != row[2] or app._sha(op) != row[3] or row[1] != op['client_op_id']
            or len(anchors) != 1 or tuple(anchors[0]) != (row[0], row[3])
            or row[0] != op['proposal']['delivery_uid'] or cert.encode(proposal) != cert.encode(op['proposal'])):
        raise ValueError('final application frozen custody differs')
    return op


def freeze(conn, proof, *, delivery_uid, application_version=1):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_sync import uuid7
    install(conn)
    prior = saved(conn, proof['intent']['intent_uid'])
    if prior is not None:
        if prior['proposal']['delivery_uid'] != delivery_uid or cert.encode(prior['proposal']['consumption']) != cert.encode(proof):
            raise ValueError('final application replay differs')
        return prior
    proposal = (prepared.prepare(conn, proof, delivery_uid=delivery_uid) if application_version == 1 else
                prepared.prepare(conn, proof, delivery_uid=delivery_uid, application_version=application_version))
    with kb.write_txn(conn):
        prior = saved(conn, proof['intent']['intent_uid'])
        if prior is not None:
            return prior
        if cert.encode(prepared.verified_prepared(conn, proof['intent']['intent_uid'])) != cert.encode(proposal):
            raise ValueError('final application proposal changed')
        app._delivery(conn, proof, delivery_uid)
        op = _wire(dict(client_op_id=uuid7(), op='apply_owner_release', task=proof['intent']['task_id'],
                        intent_uid=proof['intent']['intent_uid'], proposal=proposal))
        raw, digest = cert.encode(op), app._sha(op)
        cert.parse(raw, max_bytes=MAX_BYTES)
        token = _PERMIT.set(conn)
        try:
            conn.execute(f'INSERT INTO {TABLE} VALUES (?,?,?,?,?)', (op['intent_uid'], delivery_uid, op['client_op_id'], raw, digest))
            changed = conn.execute(f'UPDATE {queue.TABLE} SET application_digest=? WHERE delivery_uid=? '
                                   'AND application_digest IS NULL AND stage=\'consumed_pending_apply\'', (digest, delivery_uid))
            if changed.rowcount != 1:
                raise ValueError('final application anchor changed')
        finally:
            _PERMIT.reset(token)
    return saved(conn, op['intent_uid'])


def _rows(rows, source, *, event, body, home):
    fields = cert._EVENT if event else cert._COMMENT
    if not isinstance(rows, list) or len(rows) != len(source):
        raise ValueError('canonical application row inventory differs')
    for row, original in zip(rows, source):
        if (not cert._exact(row, fields) or row['uid'] != original['uid'] or row['task_id'] != body['task_id']
                or row['origin_gateway'] != home or row['deleted_at'] is not None
                or not all(cert._integer(row[k], 1) for k in ('seq', 'version'))
                or not cert._integer(row['updated_at']) or not isinstance(row['hlc'], str)
                or re.fullmatch(r'[0-9]{13}-[0-9]{4}-server', row['hlc']) is None):
            raise ValueError('canonical application row certification differs')
        keys = ('kind', 'payload', 'created_at') if event else ('author', 'body', 'created_at')
        if any(cert.encode(row[k]) != cert.encode(original[k]) for k in keys):
            raise ValueError('canonical application row payload differs')
        if event and (row['run_uid'] is not None or not cert._integer(row['ord'], 1)):
            raise ValueError('canonical application event identity differs')
    if event and rows and [r['ord'] for r in rows] != list(range(rows[0]['ord'], rows[0]['ord'] + len(rows))):
        raise ValueError('canonical application event order differs')
    if any(a['seq'] >= b['seq'] for a, b in pairwise(rows)):
        raise ValueError('canonical application sequence differs')


def validate(value, op):
    """Only use with authenticated private GET; public rows cannot certify it."""
    try:
        return _validate(value, op)
    except (KeyError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError('invalid canonical application certificate structure') from exc


def _validate(value, op):
    _wire(op)
    value = cert.parse(cert.encode(value), max_bytes=MAX_BYTES)
    version = op['proposal']['version']
    fields = {'version', 'operation', 'consumption', 'task', 'events', 'comments'}
    if (not cert._exact(value, fields | ({'allowance'} if version == 1 else {'governance'}))
            or type(value['version']) is not int or value['version'] != version
            or cert.encode(value['operation']) != cert.encode(op)
            or cert.encode(value['consumption']) != cert.encode(op['proposal']['consumption'])):
        raise ValueError('canonical application operation differs')
    proof = value['consumption']
    body = proof['intent']
    home = proof['renewal']['origin_gateway']
    _rows(value['events'], op['proposal']['events'], event=True, body=body, home=home)
    _rows(value['comments'], op['proposal']['comments'], event=False, body=body, home=home)
    task = value['task']
    if not isinstance(task, dict) or task.get('uid') != body['task_id'] or task.get('status') != 'ready':
        raise ValueError('canonical application historical task differs')
    if version == 2:
        governance = value['governance']
        if cert._exact(governance, {'state'}) and governance['state'] == 'ungoverned':
            return value
        if not cert._exact(governance, {'state', 'allowance'}) or governance['state'] != 'governed':
            raise ValueError('canonical application governance differs')
        allowance = governance['allowance']
    else:
        allowance = value['allowance']
    if (not cert._exact(allowance, {'window_uid', 'episode_uid', 'runs', 'used', 'remaining'})
            or not cert.intent._coordinate(allowance['window_uid']) or allowance['episode_uid'] != body['episode']
            or not all(cert._integer(allowance[k]) for k in ('runs', 'used', 'remaining'))
            or not 1 <= allowance['remaining'] <= body['runs']
            or allowance['used'] + allowance['remaining'] != allowance['runs']):
        raise ValueError('canonical application protected allowance differs')
    return value
