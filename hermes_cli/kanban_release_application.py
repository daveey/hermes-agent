"""Immutable HOME APPLICATION sidecar; consumption alone never supplies coverage.

Only the HOME application assembler may enter _writer, after network verification
and live vetoes. No production caller is installed yet. The writer is a transaction
custody boundary, not caller authentication; this module cannot release a task.
SQL-only readers require private consumption AND every frozen local effect.
"""
from __future__ import annotations

import contextvars
import hashlib
import sqlite3
from contextlib import contextmanager

from hermes_cli import kanban_release_certificate as cert, kanban_release_replica as replica
from hermes_cli.kanban_containment_functions import register as register_function

TABLE = 'owner_release_applied_receipts'
EVENT_FIELDS = ('uid', 'task_id', 'kind', 'payload', 'created_at', 'origin_gateway')
_RESULT_FIELDS = {'version', 'intent_uid', 'consume_uid', 'delivery_uid', 'outcome', 'mandate', 'effect_uids'}
_PERMIT = contextvars.ContextVar('owner_release_application_writer', default=None)
_INVOCATION = contextvars.ContextVar('owner_release_application_invocation', default=None)


def native_record(payload):
    """A malformed native sibling must not downgrade to legacy comment authority."""
    return any(k.startswith('owner_release_') or k == 'consume_uid' for k in payload)


def _sha(value):
    return hashlib.sha256(cert.encode(value).encode('ascii')).hexdigest()


def install(conn):
    register_function(conn, 'kanban_owner_release_application_write', 0,
                      lambda: int(_PERMIT.get() is conn and conn.in_transaction))
    conn.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} (intent_uid TEXT PRIMARY KEY, consume_uid TEXT NOT NULL UNIQUE, '
                 'delivery_uid TEXT NOT NULL UNIQUE, board TEXT NOT NULL, task_id TEXT NOT NULL, '
                 'evidence TEXT NOT NULL, digest TEXT NOT NULL)')
    abort = "BEGIN SELECT RAISE(ABORT,'owner release application receipt is immutable'); END"
    for op in ('UPDATE', 'DELETE'):
        conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_{op.lower()} BEFORE {op} ON {TABLE} {abort}')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_insert BEFORE INSERT ON {TABLE} '
                 f'WHEN NOT kanban_owner_release_application_write() {abort}')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_replace BEFORE INSERT ON {TABLE} '
                 f'WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE intent_uid=NEW.intent_uid OR consume_uid=NEW.consume_uid '
                 f'OR delivery_uid=NEW.delivery_uid OR rowid=NEW.rowid) {abort}')
    conn.execute(f'CREATE INDEX IF NOT EXISTS idx_owner_release_applied_task ON {TABLE}(task_id)')


@contextmanager
def _writer(conn):
    """Own ONE canonical writer, never borrow a caller's arbitrary transaction.

    Future application assembly performs its live vetoes and all effects here,
    then _record last. Any later refusal must raise, rolling back the whole cut.
    No network may be called while this reservation is held.
    """
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        token = _PERMIT.set(conn)
        invocation = _INVOCATION.set(object())
        try:
            yield
        finally:
            _INVOCATION.reset(invocation)
            _PERMIT.reset(token)


def _event(conn, uid):
    # Explicit fields support execute-only Rows and ignore remapped local IDs.
    row = conn.execute('SELECT uid,task_id,kind,payload,created_at,origin_gateway,run_id FROM task_events '
                       "WHERE uid=? AND typeof(payload)='text' AND length(CAST(payload AS BLOB))<=?",
                       (uid, cert.MAX_BYTES)).fetchone()
    if row is None or row[6] is not None:
        raise ValueError('application effect missing or belongs to a run')
    event = dict(zip(EVENT_FIELDS, tuple(row)[:6]))
    if (not all(isinstance(event[k], str) for k in ('uid', 'task_id', 'kind', 'payload'))
            or event['origin_gateway'] is not None and not isinstance(event['origin_gateway'], str)
            or type(event['created_at']) is not int or event['created_at'] < 0):
        raise ValueError('invalid application event identity')
    cert.parse(event['payload'])
    return event


def _result(event, proof, delivery):
    p = cert.parse(event['payload'])
    body = proof['intent']
    if (event['kind'] != 'owner_release_applied' or event['task_id'] != body['task_id']
            or event['origin_gateway'] not in (None, proof['renewal']['origin_gateway'])
            or not cert._exact(p, _RESULT_FIELDS) or type(p['version']) is not int or p['version'] != 1
            or p['intent_uid'] != body['intent_uid'] or p['consume_uid'] != proof['consume']['client_op_id']
            or p['delivery_uid'] != delivery or p['outcome'] not in ('released', 'waiting_on_parents')
            or not isinstance(p['mandate'], str) or not p['mandate'].strip() or len(p['mandate']) > 16000):
        raise ValueError('application result binding differs')
    uids = p['effect_uids']
    if (not isinstance(uids, list) or not 2 <= len(uids) <= 35 or not all(cert.intent._coordinate(uid) for uid in uids)
            or len(uids) != len(set(uids)) or {event['uid'], proof['renewal']['uid']} & set(uids)):
        raise ValueError('application effect identities differ')
    return p


def _effects(events, proof):
    body, actor = proof['intent'], proof['intent']['actor']
    expected = {'version': 1, 'owner_release_intent_uid': body['intent_uid'],
                'consume_uid': proof['consume']['client_op_id'], 'actor_kind': 'agent',
                'actor_gateway': actor['gateway'], 'actor_profile': actor['profile'],
                'actor_session': actor['session'], 'authority_kind': 'chat'}
    episodes, kinds = [], []
    for event in events:
        if event['task_id'] != body['task_id'] or event['origin_gateway'] not in (None, proof['renewal']['origin_gateway']):
            raise ValueError('application effect belongs to another HOME or task')
        p = cert.parse(event['payload'])
        kinds.append(event['kind'])
        if event['kind'] == 'stop_released':
            if any(cert.encode(p.get(k)) != cert.encode(v) for k, v in expected.items()):
                raise ValueError('application release actor or intent differs')
            episodes.append(p.get('episode'))
    if (sorted(episodes, key=str) != sorted([body['episode'], *body['capability_episodes']])
            or kinds.count('promoted_manual') != 1 or kinds.count('spend_budget_set') > 1
            or set(kinds) - {'stop_released', 'spend_budget_set', 'promoted_manual'}):
        raise ValueError('application effects do not match exact selected episodes and placement')


def _delivery(conn, proof, delivery_uid):
    from hermes_cli import kanban_release_home_queue as queue
    row = conn.execute(f'SELECT delivery_uid,board,task_id,payload,created_at,intent_uid,stage,consume_wire,wire_digest,checked '
                       f'FROM {queue.TABLE} WHERE delivery_uid=? AND '
                       'length(CAST(payload AS BLOB))<=? AND length(CAST(consume_wire AS BLOB))<=?',
                       (delivery_uid, queue.LIMIT, cert.MAX_BYTES)).fetchone()
    if row is None:
        raise ValueError('application delivery custody missing')
    # Rows adapters may wrap a tuple connection; do not depend on row_factory.
    columns = ('delivery_uid', 'board', 'task_id', 'payload', 'created_at', 'intent_uid',
               'stage', 'consume_wire', 'wire_digest', 'checked')
    row = dict(zip(columns, tuple(row)))
    body = proof['intent']
    p = cert.parse(row['payload'])
    if (row['stage'] != 'consumed_pending_apply' or row['board'] != body['board']
            or row['task_id'] != body['task_id'] or row['intent_uid'] != body['intent_uid']
            or cert.encode(queue.wire(row)) != cert.encode(proof['consume'])
            or cert.encode(p.get('intent')) != cert.encode(body)):
        raise ValueError('application delivery is not the exact winning consume')
    carrier = _event(conn, delivery_uid)
    if (carrier['kind'] != 'operator_call_requested' or carrier['task_id'] != row['task_id']
            or carrier['origin_gateway'] != body['actor']['gateway'] or carrier['payload'] != row['payload']
            or carrier['created_at'] != row['created_at']):
        raise ValueError('application delivery event differs from frozen custody')
    return carrier


def _validated(conn, proof, delivery, result, effects):
    if (not cert.intent._coordinate(delivery) or delivery in {proof['intent']['intent_uid'], proof['consume']['client_op_id']}
            or not cert.event_matches(_event(conn, proof['renewal']['uid']), proof)):
        raise ValueError('application lacks exact winning renewal')
    p = _result(result, proof, delivery)
    if [e['uid'] for e in effects] != p['effect_uids']:
        raise ValueError('application effect order differs')
    _effects(effects, proof)
    # Recheck both halves on every read; a public event never repairs a sidecar.
    for event in [result, *effects]:
        current = _event(conn, event['uid'])
        # Local append_event has no Board origin until its authenticated echo.
        # Only a privately captured HOME application permits this one-way echo;
        # canonical renewal/carrier origins remain exact and are never normalized.
        if event['origin_gateway'] is None and current['origin_gateway'] == proof['renewal']['origin_gateway']:
            current = {**current, 'origin_gateway': None}
        if cert.encode(current) != cert.encode(event):
            raise ValueError('application frozen event differs')
    return p


def _record(conn, *, consume_uid, delivery_uid, result_uid):
    """Last write in the actual application transaction; never a cache backfill."""
    if _PERMIT.get() is not conn or not conn.in_transaction:
        raise ValueError('receipt requires its own application writer')
    proof = replica.verified_consumption(conn, consume_uid)
    if proof is None:
        raise ValueError('application lacks private winning consumption')
    prior = verified_application(conn, proof['intent']['intent_uid'])
    if prior is not None:
        if prior['delivery_uid'] != delivery_uid or prior['result_event']['uid'] != result_uid:
            raise ValueError('application replay identity differs')
        return
    local = conn.execute('SELECT board_id,gateway_id,mode FROM kanban_sync_state LIMIT 1').fetchone()
    if local is None or tuple(local) != (proof['intent']['board'], proof['renewal']['origin_gateway'], 'primary'):
        raise ValueError('application writer is not the exact primary HOME')
    result = _event(conn, result_uid)
    p = _result(result, proof, delivery_uid)
    effects = [_event(conn, uid) for uid in p['effect_uids']]
    _validated(conn, proof, delivery_uid, result, effects)
    body = proof['intent']
    carrier = _delivery(conn, proof, delivery_uid)
    evidence = {'version': 1, 'consumption_sha256': _sha(proof), 'delivery': carrier, 'result': result, 'effects': effects}
    raw = cert.encode(evidence)
    cert.parse(raw)  # cap complete result+effect envelope, not each half alone
    values = (body['intent_uid'], consume_uid, delivery_uid, body['board'], body['task_id'], raw, _sha(evidence))
    prior = conn.execute(f'SELECT intent_uid,consume_uid,delivery_uid,board,task_id,evidence,digest FROM {TABLE} '
                         'WHERE intent_uid=? OR consume_uid=? OR delivery_uid=?', values[:3]).fetchall()
    if prior:
        if len(prior) != 1 or tuple(prior[0]) != values:
            raise ValueError('application receipt conflict')
        return
    conn.execute(f'INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?)', values)


def verified_application(conn, intent_uid):
    """SQL-only historical APPLIED proof. Missing optional tables fail closed.

    No task/status/clock/owner read: successor state cannot alter historical result
    identity. Live admission remains the application's separate precommit duty.
    """
    try:
        return _read_application(conn, intent_uid)
    except sqlite3.Error:
        return None


def _read_application(conn, intent_uid):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return None
    row = conn.execute(f'SELECT intent_uid,consume_uid,delivery_uid,board,task_id,evidence,digest FROM {TABLE} '
                       "WHERE intent_uid=? AND typeof(evidence)='text' AND length(CAST(evidence AS BLOB))<=?",
                       (intent_uid, cert.MAX_BYTES)).fetchone()
    if row is None or row[3] != replica._board(conn):
        return None
    try:
        proof = replica.verified_consumption(conn, row[1])
        if proof is None or (row[0], row[3], row[4]) != (proof['intent']['intent_uid'], proof['intent']['board'], proof['intent']['task_id']):
            return None
        data = cert.parse(row[5])
        if (not cert._exact(data, {'version', 'consumption_sha256', 'delivery', 'result', 'effects'})
                or type(data['version']) is not int or data['version'] != 1
                or data['consumption_sha256'] != _sha(proof) or _sha(data) != row[6]
                or cert.encode(_delivery(conn, proof, row[2])) != cert.encode(data['delivery'])):
            return None
        p = _validated(conn, proof, row[2], data['result'], data['effects'])
        return {'intent': proof['intent'], 'consumption': proof, 'delivery_uid': row[2],
                'result': p, 'result_event': data['result'], 'effects': data['effects']}
    except (TypeError, ValueError, KeyError, UnicodeError, RecursionError, sqlite3.Error):
        return None


def consumer_application(conn, intent_uid):
    """HOME proof stays private; foreign readers use a separate certified cache."""
    from hermes_cli import kanban_release_executor as executor
    return verified_application(conn, intent_uid) or executor.verified_application(conn, intent_uid)


def verified_renewal(conn, event):
    """Explicit intent-backed context, never pretend the intent is a comment."""
    try:
        p = cert.parse(event['payload'])
        uid = p.get('owner_release_intent_uid')
        if not cert.intent._coordinate(uid):
            return None
        app = consumer_application(conn, uid)
        if app is None:
            return None
        renewal = app.get('renewal') or app.get('consumption', {}).get('renewal')
        from hermes_cli import kanban_release_public as public
        matched = (cert.event_matches(event, app['consumption']) if 'consumption' in app
                   else renewal is not None and public.matches(event, renewal))
        if not matched:
            return None
        return {'payload': cert.parse(renewal['payload']),
                'mandate': {'kind': 'owner_release_intent', 'intent_uid': uid,
                            'body': app['result']['mandate']}}
    except (TypeError, ValueError, KeyError, UnicodeError, RecursionError):
        return None


def verified_release(conn, event):
    """An exact native release effect, not a HOME-labelled public lookalike."""
    try:
        p = cert.parse(event['payload'])
        uid = p.get('owner_release_intent_uid')
        if not cert.intent._coordinate(uid) or event.get('run_id') is not None or event.get('deleted_at') is not None:
            return None
        app = consumer_application(conn, uid)
        if app is None:
            return None
        current = _event(conn, event['uid'])
        if cert.encode(current) != cert.encode({k: event.get(k) for k in EVENT_FIELDS}):
            return None
        return app if any(e['uid'] == event['uid'] and e['kind'] == 'stop_released' for e in app['effects']) else None
    except (TypeError, ValueError, KeyError, UnicodeError, RecursionError, sqlite3.Error):
        return None
