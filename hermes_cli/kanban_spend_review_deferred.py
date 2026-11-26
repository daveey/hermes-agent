"""Never-submitted semantic decisions awaiting prerequisite custody. [daveey]

Keep these OUT of spend_review_requests: older senders must never mistake an
uncaptured decision for a captured legacy request. Wire/UID remain immutable.
"""
from __future__ import annotations

import json

TABLE = 'spend_review_deferred1'


def exists(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone())


def install(conn):
    conn.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} (task_id TEXT NOT NULL, episode TEXT NOT NULL, '
                 'request TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0, captured INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(task_id,episode))')
    conn.execute(f'CREATE INDEX IF NOT EXISTS {TABLE}_open ON {TABLE}(task_id,episode) WHERE closed=0')
    for action in ('UPDATE OF task_id,episode,request', 'DELETE'):
        suffix = 'update' if action.startswith('UPDATE') else 'delete'
        conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_{suffix} BEFORE {action} ON {TABLE} '
                     "BEGIN SELECT RAISE(ABORT,'deferred decision immutable'); END")
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_insert BEFORE INSERT ON {TABLE} '
                 f'WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE task_id=NEW.task_id AND episode=NEW.episode) '
                 "BEGIN SELECT RAISE(ABORT,'deferred decision immutable'); END")
    # A pre-upgrade sender can neither see this as legacy nor overwrite it with
    # a different judgment/UID after custody clears. New capture retains wire.
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {TABLE}_binding BEFORE INSERT ON spend_review_requests '
                 f'WHEN EXISTS(SELECT 1 FROM {TABLE} d WHERE d.task_id=NEW.task AND d.episode=NEW.episode '
                 "AND (d.captured!=1 OR json_extract(d.request,'$.wire') IS NOT json_extract(NEW.request,'$.wire'))) "
                 "BEGIN SELECT RAISE(ABORT,'deferred decision identity changed'); END")


def get(conn, task, episode):
    if not exists(conn):
        return None
    row = conn.execute(f'SELECT request FROM {TABLE} WHERE task_id=? AND episode=?', (task, episode)).fetchone()
    return json.loads(row[0]) if row else None


def open_keys(conn):
    if not exists(conn):
        return []
    return [tuple(row) for row in conn.execute(f'SELECT task_id,episode FROM {TABLE} WHERE closed=0')]


def save(conn, task, episode, request):
    install(conn)
    conn.execute(f'INSERT INTO {TABLE}(task_id,episode,request) VALUES (?,?,?)',
                 (task, episode, json.dumps(request, ensure_ascii=False)))


def close(conn, keys):
    if exists(conn):
        conn.executemany(f'UPDATE {TABLE} SET closed=1 WHERE task_id=? AND episode=?', keys)
