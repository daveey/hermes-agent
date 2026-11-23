"""Bounded current deferred membership reads; no row-success inference. [daveey]

The future origin_pending1 read is capability gated. One task is visited per push,
with durable rotation even on failure. Five-minute episode diagnostics remain a
separate consumer; these observations never resolve business custody.
"""
from urllib.parse import quote
from uuid import uuid4

from hermes_cli import kanban_replay_deferred as deferred
from hermes_cli import kanban_replay_journal as journal
from hermes_cli.kanban_db_connect import write_txn


_SCHEMA = '''CREATE TABLE IF NOT EXISTS kanban_replay_observation1 (
    board_incarnation TEXT NOT NULL, task TEXT NOT NULL, origin TEXT NOT NULL,
    hlc TEXT NOT NULL, gateway TEXT NOT NULL, run TEXT, status TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL, PRIMARY KEY(board_incarnation,task,origin)
)'''
_CURSOR = '''CREATE TABLE IF NOT EXISTS kanban_replay_observation_cursor1 (
    id INTEGER PRIMARY KEY CHECK(id=1), task TEXT NOT NULL, ticket INTEGER NOT NULL
)'''
_READS = '''CREATE TABLE IF NOT EXISTS kanban_replay_observation_reads1 (
    board_incarnation TEXT NOT NULL, task TEXT NOT NULL, token TEXT NOT NULL,
    PRIMARY KEY(board_incarnation,task)
)'''


def observed(conn):
    """Last valid complete task reads; possibly stale, never freshness/timing proof."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='kanban_replay_observation1'").fetchone():
        return []
    return [dict(row) for row in conn.execute('SELECT * FROM kanban_replay_observation1 '
                                              'ORDER BY board_incarnation,task,origin')]


def _binding(local):
    return tuple(local[key] for key in ('board_incarnation', 'gateway_principal', 'journal_id', 'generation'))


def _candidate(conn, board, after):
    candidates = []
    for table in ('kanban_replay_deferred1', 'kanban_replay_observation1'):
        row = conn.execute(f'SELECT task FROM {table} WHERE board_incarnation=? AND task>? '
                            'ORDER BY task LIMIT 1', (board, after)).fetchone()
        if row:
            candidates.append(row[0])
    return min(candidates) if candidates else None


def _reserve(conn, local):
    with write_txn(conn):
        conn.execute(_SCHEMA)
        conn.execute(_CURSOR)
        conn.execute(_READS)
        deferred.install(conn)
        previous = conn.execute('SELECT task,ticket FROM kanban_replay_observation_cursor1 WHERE id=1').fetchone()
        board = local['board_incarnation']
        task = _candidate(conn, board, previous['task'] if previous else '') or _candidate(conn, board, '')
        if task is None:
            return None
        ticket = previous['ticket'] + 1 if previous else 1
        conn.execute('INSERT INTO kanban_replay_observation_cursor1 VALUES (1,?,?) '
                     'ON CONFLICT(id) DO UPDATE SET task=excluded.task,ticket=excluded.ticket', (task, ticket))
        token = uuid4().hex
        conn.execute('INSERT INTO kanban_replay_observation_reads1 VALUES (?,?,?) '
                     'ON CONFLICT(board_incarnation,task) DO UPDATE SET token=excluded.token', (board, task, token))
        captures = deferred.captures(conn, board, task)
        return task, token, captures


def _store(conn, local, task, reply, captures):
    conn.execute('DELETE FROM kanban_replay_observation1 WHERE board_incarnation=? AND task=?',
                 (local['board_incarnation'], task))
    for entry in reply['entries']:
        conn.execute('INSERT INTO kanban_replay_observation1 VALUES (?,?,?,?,?,?,?,?)',
                     (local['board_incarnation'], task, entry['origin'], entry['hlc'], entry['gateway'],
                      entry['run'], entry['status'], entry['evidence_sha256']))
    origins = {entry['origin'] for entry in reply['entries']}
    deferred.retire_absent(conn, local['board_incarnation'], task, origins, captures)


def after_push(conn, hub, root, local, cap):
    """An optional read/storage failure must not fail already committed transport."""
    import logging
    import sqlite3
    log = logging.getLogger(__name__)
    try:
        result = observe_one(conn, hub, root, local, cap)
    except sqlite3.Error as exc:
        log.warning('Deferred observation storage unavailable: %s', exc)
        return
    if result['status'] == 'unavailable' and result.get('task'):
        log.warning('Deferred observation unavailable for %s: %s', result['task'], result.get('reason', 'unknown'))


def _release(conn, board, task, token):
    conn.execute('DELETE FROM kanban_replay_observation_reads1 '
                 'WHERE board_incarnation=? AND task=? AND token=?', (board, task, token))


def observe_one(conn, hub, root, local, cap):
    """Observe at most one tracked task outside the writer; failure never means empty."""
    from hermes_cli.kanban_sync_client import HubError
    if cap.get('deferred_observation') != 'origin_pending1':
        return dict(status='unavailable')
    reserved = _reserve(conn, local)
    if reserved is None:
        return dict(status='idle')
    task, token, captures = reserved
    try:
        reply = hub._call('GET', root + 'deferred?task=' + quote(task, safe=''))
        deferred.validate_membership(reply, local, task)
    except (OSError, ValueError, HubError) as exc:
        with write_txn(conn):
            _release(conn, local['board_incarnation'], task, token)
        return dict(status='unavailable', task=task, reason=str(exc)[:500])
    with write_txn(conn):
        current = conn.execute('SELECT token FROM kanban_replay_observation_reads1 '
                               'WHERE board_incarnation=? AND task=?', (local['board_incarnation'], task)).fetchone()
        if current is None or current['token'] != token or _binding(journal.snapshot(conn)) != _binding(local):
            _release(conn, local['board_incarnation'], task, token)
            return dict(status='superseded', task=task)
        _store(conn, local, task, reply, captures)
        _release(conn, local['board_incarnation'], task, token)
    return dict(status='observed', task=task)
