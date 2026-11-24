"""Append-only local intent/observation custody, not transport settlement. [daveey]

Only a future authenticated producer may call seal with a Board readback. This
module makes no network call, certifies no historical admission and never changes
outbox, events, holds, workers or sync-diff. Unsealed intents survive uncertain
HTTP outcomes for exact-request reconciliation, never replacement identity.
"""
from __future__ import annotations

import sqlite3

from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_history_residue_source import (
    Refused, _positive, _require, _text, decode_source, strict_object, validate_receipt,
)

INTENTS = 'kanban_history_residue_intents'
RECEIPTS = 'kanban_history_residue_receipts'


def _scope(scope):
    _require(isinstance(scope, tuple) and len(scope) == 3 and all(_text(v) for v in scope), 'scope_invalid')
    for value in scope:
        try:
            value.encode('utf-8')
        except UnicodeError as exc:
            raise Refused('scope_invalid') from exc
    return scope


def _request(raw):
    request = strict_object(raw, max_bytes=2 * 1024 * 1024)
    _require(set(request) == {'version', 'task', 'expected_version', 'source_json', 'reason'}
             and type(request['version']) is int and request['version'] == 1
             and _positive(request['expected_version']) and _text(request['reason']), 'request_invalid')
    row = decode_source(request['source_json'])
    _require(strict_object(row['fields'])['task_id'] == request['task'], 'request_task')
    return request, row


def _schema(conn):
    conn.execute(f'CREATE TABLE IF NOT EXISTS {INTENTS} (endpoint TEXT NOT NULL, board TEXT NOT NULL, '
                 'gateway TEXT NOT NULL, op_id TEXT NOT NULL, event_uid TEXT NOT NULL, request_json TEXT NOT NULL, '
                 'PRIMARY KEY(endpoint,board,gateway,op_id), UNIQUE(endpoint,board,gateway,event_uid))')
    conn.execute(f'CREATE TABLE IF NOT EXISTS {RECEIPTS} (endpoint TEXT NOT NULL, board TEXT NOT NULL, '
                 'gateway TEXT NOT NULL, op_id TEXT NOT NULL, receipt_json TEXT NOT NULL, '
                 'PRIMARY KEY(endpoint,board,gateway,op_id))')
    for table in (INTENTS, RECEIPTS):
        for op in ('UPDATE', 'DELETE'):
            conn.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_{op.lower()} BEFORE {op} ON {table} '
                         "BEGIN SELECT RAISE(ABORT,'immutable history observation custody'); END")
        extra = ' OR event_uid=NEW.event_uid' if table == INTENTS else ''
        conn.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_insert BEFORE INSERT ON {table} '
                     f'WHEN EXISTS (SELECT 1 FROM {table} WHERE endpoint=NEW.endpoint AND board=NEW.board '
                     f'AND gateway=NEW.gateway AND (op_id=NEW.op_id{extra})) '
                     "BEGIN SELECT RAISE(ABORT,'immutable history observation custody'); END")


def lookup(conn, scope, op_id):
    """Historical bytes only. Missing custody is unknown, not fresh clearance."""
    scope = _scope(scope)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (INTENTS,)).fetchone():
        return None
    row = conn.execute(f'SELECT request_json FROM {INTENTS} WHERE endpoint=? AND board=? AND gateway=? AND op_id=?',
                       (*scope, op_id)).fetchone()
    if row is None:
        return None
    receipt = conn.execute(f'SELECT receipt_json FROM {RECEIPTS} WHERE endpoint=? AND board=? AND gateway=? AND op_id=?',
                           (*scope, op_id)).fetchone()
    return {'request_json': row[0], 'receipt_json': receipt[0] if receipt else None}


def stage(conn, scope, request_json):
    """Commit exact intent before network I/O. Caller must separately prove live inputs."""
    scope = _scope(scope)
    _, row = _request(request_json)
    _require(not conn.in_transaction, 'custody_nested_transaction')
    with write_txn(conn):
        _schema(conn)
        old = lookup(conn, scope, row['op_id'])
        if old is not None:
            _require(old['request_json'] == request_json, 'intent_conflict')
        else:
            conn.execute(f'INSERT INTO {INTENTS} VALUES(?,?,?,?,?,?)', (*scope, row['op_id'], row['row_uid'], request_json))
    return row['op_id']


def seal(conn, scope, op_id, receipt_json):
    """Store exact authenticated readback; this is not current evidence or sync ACK."""
    scope = _scope(scope)
    _require(not conn.in_transaction, 'custody_nested_transaction')
    with write_txn(conn):
        old = lookup(conn, scope, op_id)
        if old is None:
            raise Refused('intent_missing')
        request, source = _request(old['request_json'])
        _require(source['op_id'] == op_id, 'intent_identity')
        receipt = strict_object(receipt_json, max_bytes=2 * 1024 * 1024)
        validate_receipt(receipt, request['source_json'], gateway=scope[2], board=scope[1])
        _require(receipt['reason'] == request['reason'] and receipt['parent'].get('version') == request['expected_version']
                 and type(receipt['parent'].get('version')) is int, 'receipt_request_conflict')
        if old['receipt_json'] is not None:
            _require(old['receipt_json'] == receipt_json, 'receipt_conflict')
        else:
            conn.execute(f'INSERT INTO {RECEIPTS} VALUES(?,?,?,?,?)', (*scope, op_id, receipt_json))
