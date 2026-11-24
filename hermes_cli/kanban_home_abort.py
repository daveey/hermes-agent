"""Durable exact-attempt noncommit receipts; never timeout-based release. [daveey]

The owner obtains these only by authenticated Board abort RPC. Board atomically
refuses abort after commit and remembers the negative decision against delayed
prepare/commit packets. This local ledger does not release any other restriction.
"""
from __future__ import annotations

import json

_KEYS = ('task', 'source', 'destination', 'nonce', 'expected_version')


def predicate(prepared: str = 'kanban_home_prepared1') -> str:
    return (f'a.task_id={prepared}.task_id AND a.nonce={prepared}.nonce '
            f"AND json_extract(a.receipt,'$.source') IS {prepared}.source "
            f"AND json_extract(a.receipt,'$.destination') IS {prepared}.destination "
            f"AND json_extract(a.receipt,'$.expected_version') IS {prepared}.expected_version")


def install(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS kanban_home_aborted1 (
        nonce TEXT PRIMARY KEY, task_id TEXT NOT NULL, receipt TEXT NOT NULL,
        CHECK(json_valid(receipt) AND json_extract(receipt,'$.task') IS task_id
          AND json_extract(receipt,'$.nonce') IS nonce AND json_extract(receipt,'$.phase') IS 'aborted'
          AND json_type(receipt,'$.rows') IS 'array' AND json_array_length(receipt,'$.rows') IS 0))""")
    for action in ('UPDATE', 'DELETE'):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS home_aborted1_{action.lower()} BEFORE {action} ON kanban_home_aborted1 "
                     "BEGIN SELECT RAISE(ABORT,'home abort immutable'); END")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS home_aborted1_insert BEFORE INSERT ON kanban_home_aborted1
        WHEN EXISTS(SELECT 1 FROM kanban_home_aborted1 WHERE nonce=NEW.nonce)
          OR NOT EXISTS(SELECT 1 FROM kanban_home_prepared1 p WHERE p.task_id=NEW.task_id AND p.nonce=NEW.nonce
            AND p.source IS json_extract(NEW.receipt,'$.source')
            AND p.destination IS json_extract(NEW.receipt,'$.destination')
            AND p.expected_version IS json_extract(NEW.receipt,'$.expected_version')
            AND json_type(NEW.receipt,'$.expected_version') IS 'integer')
          OR EXISTS(SELECT 1 FROM kanban_home_finalized1 WHERE task_id=NEW.task_id)
        BEGIN SELECT RAISE(ABORT,'home abort scope or immutable custody'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS home_finalized1_not_aborted BEFORE INSERT ON kanban_home_finalized1
        WHEN EXISTS(SELECT 1 FROM kanban_home_aborted1 WHERE task_id=NEW.task_id AND nonce=NEW.nonce)
        BEGIN SELECT RAISE(ABORT,'home transfer already aborted'); END""")


def aborted(conn, task_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_home_aborted1'").fetchone():
        return False
    return bool(conn.execute('SELECT 1 FROM kanban_home_prepared1 WHERE task_id=? '
        f'AND EXISTS(SELECT 1 FROM kanban_home_aborted1 a WHERE {predicate()})', (task_id,)).fetchone())


def abort_source(conn, record):
    """Persist a trusted transport response, not caller-asserted failure prose."""
    if not conn.in_transaction:
        raise ValueError('home abort requires writer transaction')
    if (not isinstance(record, dict) or record.get('phase') != 'aborted' or record.get('rows') != []
            or type(record.get('expected_version')) is not int):
        raise ValueError('home abort decision shape mismatch')
    state = conn.execute('SELECT gateway_id FROM kanban_sync_state').fetchall()
    if len(state) != 1 or state[0][0] != record.get('source'):
        raise ValueError('home abort is not on the source gateway')
    row = conn.execute('SELECT task_id,source,destination,nonce,expected_version FROM kanban_home_prepared1 WHERE task_id=?',
                       (record.get('task'),)).fetchone()
    if row is None or tuple(row) != tuple(record.get(k) for k in _KEYS):
        raise ValueError('home abort decision scope mismatch')
    if conn.execute('SELECT 1 FROM kanban_home_finalized1 WHERE task_id=?', (record['task'],)).fetchone():
        raise ValueError('home transfer already finalized')
    raw = json.dumps(record, sort_keys=True, separators=(',', ':'))
    old = conn.execute('SELECT receipt FROM kanban_home_aborted1 WHERE nonce=?', (record['nonce'],)).fetchone()
    if old:
        if old[0] != raw:
            raise ValueError('home abort decision changed')
    else:
        conn.execute('INSERT INTO kanban_home_aborted1(nonce,task_id,receipt) VALUES (?,?,?)',
                     (record['nonce'], record['task'], raw))
    return {'task': record['task'], 'nonce': record['nonce'], 'phase': 'aborted_local'}
