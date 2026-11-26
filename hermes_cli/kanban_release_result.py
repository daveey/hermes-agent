"""Origin-only historical completion custody, never executor clearance.

A private Board application is bound to the original native delivery/read ACK.
No HOME queue, task postimage, effect rows or allowance is installed at origin.
"""
from __future__ import annotations

import contextvars
import hashlib

from hermes_cli import kanban_release_certificate as cert, kanban_release_intent as intent
from hermes_cli import kanban_release_final as final
from hermes_cli.kanban_containment_functions import register as register_function

TABLE = 'native_owner_release_results'
_PERMIT = contextvars.ContextVar('native_owner_release_result_write', default=None)


def _digest(value):
    return hashlib.sha256(cert.encode(value).encode('ascii')).hexdigest()


def install(conn):
    if 'result_digest' not in {r[1] for r in conn.execute('PRAGMA table_info(native_owner_release_intents)')}:
        conn.execute('ALTER TABLE native_owner_release_intents ADD COLUMN result_digest TEXT')
    abort = "BEGIN SELECT RAISE(ABORT,'native result custody immutable'); END"
    register_function(conn, 'kanban_native_release_result_write', 0,
                      lambda: int(_PERMIT.get() is conn and conn.in_transaction))
    conn.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} (intent_uid TEXT PRIMARY KEY,certificate TEXT NOT NULL,digest TEXT NOT NULL)')
    for op in ('UPDATE','DELETE'):
        conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_{op.lower()} BEFORE {op} ON {TABLE} {abort}')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_insert BEFORE INSERT ON {TABLE} '
                 f'WHEN NOT kanban_native_release_result_write() {abort}')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_replace BEFORE INSERT ON {TABLE} '
                 f'WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE intent_uid=NEW.intent_uid OR rowid=NEW.rowid) {abort}')
    # Restrict the UDF-bearing trigger to its guarded column: SQLite resolves
    # callback names at statement preparation even when a WHEN is false. Fresh
    # intents on ordinary reopened connections must still advance their stage.
    conn.execute('CREATE TRIGGER IF NOT EXISTS native_release_result_anchor BEFORE UPDATE OF result_digest ON native_owner_release_intents '
                 'WHEN NOT kanban_native_release_result_write() ' + abort)
    conn.execute('CREATE TRIGGER IF NOT EXISTS native_release_result_completed BEFORE UPDATE ON native_owner_release_intents '
                 'WHEN OLD.result_digest IS NOT NULL ' + abort)
    conn.execute('CREATE TRIGGER IF NOT EXISTS native_release_result_delete BEFORE DELETE ON native_owner_release_intents '
                 'WHEN OLD.result_digest IS NOT NULL ' + abort)
    conn.execute('CREATE TRIGGER IF NOT EXISTS native_release_result_replace BEFORE INSERT ON native_owner_release_intents '
                 'WHEN EXISTS(SELECT 1 FROM native_owner_release_intents WHERE result_digest IS NOT NULL '
                 'AND (intent_uid=NEW.intent_uid OR action_key=NEW.action_key OR rowid=NEW.rowid)) ' + abort)


def _local(conn, body):
    from hermes_cli import kanban_release_delivery as delivery, kanban_release_read as read
    row, captured = intent._current(conn, body)
    wire = delivery.existing(conn, body)
    if wire is None:
        raise ValueError('native completed result requires original delivery')
    payload = cert.parse(wire['fields']['payload'])
    ack = read._row(conn, body)
    if (ack is None or ack[0] != cert.encode(payload['seen_comments']) or ack[1] != payload['nonce']
            or not isinstance(ack[2], str) or not ack[2] or not isinstance(ack[3], str) or not ack[3]
            or type(ack[4]) is not int or ack[4] != 1):
        raise ValueError('native completed result read ACK differs')
    return row, captured, wire, payload


def _validate(conn, body, value):
    _, captured, wire, payload = _local(conn, body)
    if not isinstance(value, dict) or not isinstance(value.get('operation'), dict):
        raise ValueError('native completed result missing operation')
    value = final.validate(value, value['operation'])
    proof = cert.validate(value['consumption'], board=body['board'], task=body['task_id'], intent_uid=body['intent_uid'])
    if (cert.encode(proof['intent']) != cert.encode(body)
            or value['operation']['proposal']['delivery_uid'] != wire['uid']
            or cert.encode(proof['consume']['seen_comments']) != cert.encode(payload['seen_comments'])):
        raise ValueError('native completed result local identity differs')
    if captured.validated() is None:
        raise ValueError('native release custody expired during result validation')
    return value


def saved(conn, body):
    stage = intent.stage(conn, body)
    columns = {r[1] for r in conn.execute('PRAGMA table_info(native_owner_release_intents)')}
    anchor = (conn.execute('SELECT result_digest FROM native_owner_release_intents WHERE intent_uid=?',
                           (body['intent_uid'],)).fetchone()[0] if 'result_digest' in columns else None)
    exists = conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (TABLE,)).fetchone()
    row = (conn.execute(f'SELECT certificate,digest FROM {TABLE} WHERE intent_uid=? '
                        "AND typeof(certificate)='text' AND length(CAST(certificate AS BLOB))<=?",
                        (body['intent_uid'], final.MAX_BYTES)).fetchone() if exists else None)
    if row is None:
        if anchor is not None or stage == 'applied':
            raise ValueError('native completed result missing; never reconstruct')
        return None
    value = _validate(conn, body, cert.parse(row[0], max_bytes=final.MAX_BYTES))
    if stage != 'applied' or row[0] != cert.encode(value) or row[1] != _digest(value) or anchor != row[1]:
        raise ValueError('native completed result custody differs')
    return value


def _record(conn, body, value):
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        install(conn)
        prior = saved(conn, body)
        if prior is not None:
            if cert.encode(prior) != cert.encode(value):
                raise ValueError('native completed result replay differs')
            return prior
        value = _validate(conn, body, value)
        row, captured, _, _ = _local(conn, body)
        if row[3] not in {'delivery_pending','consumed'}:
            raise ValueError('native result intent no longer pending')
        token = _PERMIT.set(conn)
        try:
            raw, digest = cert.encode(value), _digest(value)
            conn.execute(f'INSERT INTO {TABLE} VALUES (?,?,?)', (body['intent_uid'], raw, digest))
            changed = conn.execute("UPDATE native_owner_release_intents SET result_digest=?,stage='applied' "
                                   'WHERE intent_uid=? AND stage=? AND result_digest IS NULL',
                                   (digest, body['intent_uid'], row[3])).rowcount
            if changed != 1:
                raise ValueError('native result journal changed')
            if captured.validated() is None:
                raise ValueError('native release custody expired before result commit')
        finally:
            _PERMIT.reset(token)
    return saved(conn, body)


def reconcile(conn, hub, body):
    from hermes_cli import kanban_release_delivery as delivery
    from hermes_cli.kanban_sync_client import HubError
    intent.stage(conn, body)
    if hub.ident.gateway != body['actor']['gateway']:
        raise ValueError('native result transport gateway differs')
    prior = saved(conn, body)
    if prior is not None:
        return reply(prior)
    if delivery.existing(conn, body) is None:
        return None
    _local(conn, body)
    try:
        value = hub.owner_release_application(body['board'], body['intent_uid'])
    except HubError as exc:
        if exc.status != 404:
            raise
        intent.stage(conn, body)
        return None
    return reply(_record(conn, body, value))


def reply(value):
    body = value['consumption']['intent']
    return {'ok': True, 'task_id': body['task_id'], 'intent_uid': body['intent_uid'], 'release_stage': 'applied',
            'historical': True, 'execution_clearance': False,
            'result': value['operation']['proposal']['application']['result']}
