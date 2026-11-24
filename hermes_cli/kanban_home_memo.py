"""Original custody for small administrative commands outside the sync outbox. [daveey]"""
from __future__ import annotations

import json

from hermes_cli import kanban_home_command as home

_TABLE = 'kanban_home_delivery1'


def install(conn):
    conn.execute(f'CREATE TABLE IF NOT EXISTS {_TABLE} (request_uid TEXT PRIMARY KEY, task_id TEXT NOT NULL, '
                 'request_json TEXT NOT NULL, settled INTEGER NOT NULL DEFAULT 0)')
    home.install(conn, _TABLE)
    for action in ('UPDATE OF request_uid,task_id,request_json', 'DELETE'):
        name = 'update' if action.startswith('UPDATE') else 'delete'
        conn.execute(f'CREATE TRIGGER IF NOT EXISTS home_delivery1_{name} BEFORE {action} ON {_TABLE} '
                     "BEGIN SELECT RAISE(ABORT,'home delivery intent immutable'); END")
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS home_delivery1_insert BEFORE INSERT ON {_TABLE} '
                 f'WHEN EXISTS(SELECT 1 FROM {_TABLE} WHERE request_uid=NEW.request_uid) '
                 "BEGIN SELECT RAISE(ABORT,'home delivery intent immutable'); END")


def get(conn, key):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (_TABLE,)).fetchone():
        return None
    row = conn.execute(f'SELECT request_json,home_generations FROM {_TABLE} WHERE request_uid=?', (key,)).fetchone()
    return home.outbound({'home_generations': row[1]}, json.loads(row[0])) if row else None


def reserve(conn, key, task, op, *, epoch=None, foreign=False):
    """An exact retry reads original bytes/epoch before any new capture."""
    if not conn.in_transaction:
        raise ValueError('home delivery reservation requires writer transaction')
    install(conn)
    old = get(conn, key)
    if old is not None:
        semantic = {k:v for k,v in old.items() if k != 'home_generations'}
        if semantic != op:
            raise ValueError('home delivery request identity changed')
        return old
    captured = epoch if foreign else home.capture(conn, op)
    raw = json.dumps(op, sort_keys=True, separators=(',', ':'), allow_nan=False)
    conn.execute(f'INSERT INTO {_TABLE}(request_uid,task_id,request_json,home_generations) VALUES (?,?,?,?)',
                 (key, task, raw, captured))
    return home.outbound({'home_generations': captured}, op)


def settled(conn, key, status):
    if status in ('applied', 'merged', 'rejected'):
        conn.execute(f'UPDATE {_TABLE} SET settled=1 WHERE request_uid=?', (key,))


def pending(conn, task):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (_TABLE,)).fetchone()
                and conn.execute(f'SELECT 1 FROM {_TABLE} WHERE task_id=? AND settled=0', (task,)).fetchone())
