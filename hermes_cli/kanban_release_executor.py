"""Immutable host-local Board-certified final application for foreign consumers.

Capture only through authenticated prewriter row_evidence; offline pickup copies
only recertified protected cache bytes. Never creates HOME consumption or queues.
Every SQL-only read rechecks all local public halves. No task/allowance writes.
"""
from __future__ import annotations

import contextvars
import sqlite3

from hermes_cli import kanban_release_application as app, kanban_release_certificate as cert
from hermes_cli import kanban_release_public as public
from hermes_cli.kanban_containment_functions import register as register_function
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from hermes_cli.kanban_sync_hub_transport import BoundedResponseTooLarge

TABLE = 'owner_release_executor_receipts'
_PERMIT = contextvars.ContextVar('owner_release_executor_write', default=None)


class ReplicaConflict(ContainmentRefused):
    """Canonical final receipt conflicts with immutable first observation."""


class ReplicaOversize(ContainmentRefused):
    """One final receipt exceeds the bound; hold that card, not its whole page."""


def install(conn):
    register_function(conn, 'kanban_owner_release_executor_write', 0,
                      lambda: int(_PERMIT.get() is conn and conn.in_transaction))
    conn.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} (intent_uid TEXT PRIMARY KEY, result_uid TEXT NOT NULL UNIQUE, '
                 'consume_uid TEXT NOT NULL UNIQUE, delivery_uid TEXT NOT NULL UNIQUE, board TEXT NOT NULL, '
                 'task_id TEXT NOT NULL, evidence TEXT NOT NULL, digest TEXT NOT NULL)')
    abort = "BEGIN SELECT RAISE(ABORT,'owner release executor receipt immutable'); END"
    for op in ('UPDATE', 'DELETE'):
        conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_{op.lower()} BEFORE {op} ON {TABLE} {abort}')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_insert BEFORE INSERT ON {TABLE} '
                 f'WHEN NOT kanban_owner_release_executor_write() {abort}')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_replace BEFORE INSERT ON {TABLE} '
                 f'WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE intent_uid=NEW.intent_uid OR result_uid=NEW.result_uid '
                 f'OR consume_uid=NEW.consume_uid OR delivery_uid=NEW.delivery_uid OR rowid=NEW.rowid) {abort}')


def _board(conn):
    row = conn.execute('SELECT board_id FROM kanban_sync_state LIMIT 1').fetchone()
    return row[0] if row else None


def _store(conn, value, receipt):
    from hermes_cli import kanban_db as kb
    body = receipt['intent']
    if _board(conn) != body['board'] or conn.execute('SELECT gateway_id FROM kanban_sync_state LIMIT 1').fetchone()[0] != value['reader_gateway']:
        raise ReplicaConflict('executor receipt board or authenticated reader differs')
    raw = cert.encode(value)
    values = (body['intent_uid'], receipt['result_event']['uid'], receipt['renewal']['uid'],
              receipt['delivery_uid'], body['board'], body['task_id'], raw, app._sha(value))
    with kb.write_txn(conn, allow_nested=True):
        install(conn)
        prior = conn.execute(f'SELECT intent_uid,result_uid,consume_uid,delivery_uid,board,task_id,evidence,digest '
                             f'FROM {TABLE} WHERE intent_uid=? OR result_uid=? OR consume_uid=? OR delivery_uid=?',
                             values[:4]).fetchall()
        if prior:
            if len(prior) != 1 or tuple(prior[0]) != values:
                raise ReplicaConflict('executor receipt identity changed')
            return
        token = _PERMIT.set(conn)
        try:
            conn.execute(f'INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?,?)', values)
        finally:
            _PERMIT.reset(token)


def _cached(conn, uid):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return None
    row = conn.execute(f'SELECT intent_uid,result_uid,consume_uid,delivery_uid,board,task_id,evidence,digest '
                       f'FROM {TABLE} WHERE (intent_uid=? OR result_uid=?) AND typeof(evidence)=\'text\' '
                       'AND length(CAST(evidence AS BLOB))<=?', (uid, uid, public.MAX_BYTES)).fetchone()
    if row is None or row[4] != _board(conn):
        return None
    value = cert.parse(row[6], max_bytes=public.MAX_BYTES)
    result = public.validate(value, board=row[4], task=row[5], uid=row[1])
    expected = (result['intent']['intent_uid'], result['result_event']['uid'], result['renewal']['uid'],
                result['delivery_uid'], result['intent']['board'], result['intent']['task_id'])
    if tuple(row)[:6] != expected or app._sha(value) != row[7]:
        raise ReplicaConflict('executor receipt cache binding differs')
    return value, result


def verified_application(conn, uid):
    """Foreign-only history; HOME must retain its stronger private assembler proof."""
    try:
        cached = _cached(conn, uid)
        if cached is None:
            return None
        value, result = cached
        local = conn.execute('SELECT gateway_id,mode FROM kanban_sync_state LIMIT 1').fetchone()
        # Exact primary HOME must never fall back if its private receipt is lost.
        if local is None or local[0] != value['reader_gateway'] or local[0] in (
                result['intent']['home'], result['renewal']['origin_gateway']):
            return None
        columns = {r[1] for r in conn.execute('PRAGMA table_info(task_events)')}
        live = ' AND deleted_at IS NULL' if 'deleted_at' in columns else ''
        for frozen in [value['delivery'], value['renewal'], *value['events']]:
            row = conn.execute('SELECT uid,task_id,kind,payload,created_at,origin_gateway,run_id FROM task_events '
                               'WHERE uid=? AND typeof(payload)=\'text\' AND length(CAST(payload AS BLOB))<=?' + live,
                               (frozen['uid'], cert.MAX_BYTES)).fetchone()
            if row is None or row[6] is not None:
                return None
            if not public.matches(dict(zip(app.EVENT_FIELDS, tuple(row)[:6])), frozen):
                return None
        return result
    except (sqlite3.Error, TypeError, ValueError, KeyError, UnicodeError, RecursionError, ReplicaConflict):
        return None


def _candidate(change):
    return (change.table == 'task_events' and change.op == 'upsert'
            and change.fields.get('kind') == 'owner_release_applied')


def certify_page(conn, hub, board, changes):
    for change in changes:
        if not _candidate(change):
            continue
        if conn.in_transaction:
            raise ContainmentRefused('executor receipt fetch requires prewriter boundary')
        try:
            reply = hub.row_evidence(board, 'task_events', change.uid, max_response_bytes=public.MAX_RESPONSE_BYTES)
        except BoundedResponseTooLarge as exc:
            raise ReplicaOversize(str(exc)) from exc
        if not isinstance(reply, dict) or 'owner_release_applied_v1' not in reply:
            continue  # old DO or public lookalike: no authority, ordinary history proceeds
        try:
            value, result = public.evidence(reply, board=board, task=change.fields.get('task_id'), uid=change.uid)
        except (ValueError, KeyError) as exc:
            raise ReplicaConflict(f'invalid canonical executor receipt: {exc}') from exc
        current = {**change.fields, 'uid': change.uid, 'origin_gateway': change.origin_gateway}
        if not public.matches(current, result['result_event']):
            raise ReplicaConflict('feed result differs from certified executor receipt')
        _store(conn, value, result)


def adopt_receipts(conn, source, changes):
    for change in changes:
        if not _candidate(change):
            continue
        try:
            cached = _cached(source, change.uid)
        except sqlite3.Error as exc:
            raise ReplicaConflict('pickup executor receipt schema unreadable') from exc
        if cached is None:
            continue
        value, result = cached
        current = {**change.fields, 'uid': change.uid, 'origin_gateway': change.origin_gateway}
        if not public.matches(current, result['result_event']):
            raise ReplicaConflict('pickup result differs from certified executor receipt')
        _store(conn, value, result)
