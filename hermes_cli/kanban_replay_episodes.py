"""Active fingerprint episodes and due diagnostics, not notice delivery. [daveey]"""
import os
from time import monotonic_ns
from uuid import uuid4


_EPOCH = uuid4().hex
_CLOCK = '''CREATE TABLE IF NOT EXISTS kanban_replay_episode_clock1 (
    id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL
)'''
_SCHEMA = '''CREATE TABLE IF NOT EXISTS kanban_replay_episode1 (
    board_incarnation TEXT NOT NULL, task TEXT NOT NULL, origin TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL, reachable_ms INTEGER NOT NULL,
    sample_token TEXT NOT NULL, completed_ms INTEGER NOT NULL,
    PRIMARY KEY(board_incarnation,task,origin)
)'''


def sample():
    """A process-local clock never connects samples across restart or fork."""
    return str(os.getpid()), monotonic_ns() // 1_000_000


def epoch(conn):
    token = conn.execute('SELECT token FROM kanban_replay_episode_clock1 WHERE id=1').fetchone()
    return _EPOCH, token[0] if token else None


def interrupt(conn):
    """Invalidate shared sampling without discarding age or failing original work."""
    import logging
    import sqlite3
    from hermes_cli.kanban_db_connect import write_txn
    global _EPOCH
    _EPOCH = uuid4().hex
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_replay_episode_clock1'").fetchone():
            return
        with write_txn(conn):
            conn.execute('INSERT INTO kanban_replay_episode_clock1 VALUES (1,?) '
                         'ON CONFLICT(id) DO UPDATE SET token=excluded.token', (_EPOCH,))
    except sqlite3.Error as exc:
        # A reserved task's durable baseline was already consumed before I/O.
        # If storage is wholly unavailable, only this process can be invalidated.
        logging.getLogger(__name__).warning('Deferred sampling interruption unavailable: %s', exc)


def begin(conn, board, task):
    """Consume <=16 prior baselines before I/O, including cross-process failures."""
    conn.execute(_SCHEMA)
    conn.execute(_CLOCK)
    previous = {row['origin']: dict(row) for row in conn.execute(
        'SELECT * FROM kanban_replay_episode1 WHERE board_incarnation=? AND task=?', (board, task))}
    conn.execute("UPDATE kanban_replay_episode1 SET sample_token='' WHERE board_incarnation=? AND task=?",
                 (board, task))
    return previous


def replace(conn, board, task, entries, started, finished, previous):
    """Atomically replace <=16 episodes alongside their complete membership read."""
    conn.execute('DELETE FROM kanban_replay_episode1 WHERE board_incarnation=? AND task=?', (board, task))
    for entry in entries:
        old = previous.get(entry['origin'])
        elapsed = 0
        if old and old['evidence_sha256'] == entry['evidence_sha256']:
            elapsed = old['reachable_ms']
            gap = started[1] - old['completed_ms']
            if (old['sample_token'] == started[0] == finished[0] and
                    0 <= gap <= 60000 and finished[1] >= started[1]):
                elapsed = min(300000, elapsed + gap)
        conn.execute('INSERT INTO kanban_replay_episode1 VALUES (?,?,?,?,?,?,?)',
                     (board, task, entry['origin'], entry['evidence_sha256'], elapsed,
                      finished[0], finished[1]))
    from hermes_cli.kanban_replay_diagnostics import synchronize
    synchronize(conn, board, task)


def diagnostics(conn):
    """Due current episodes with actionable queue evidence; not delivery receipts."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='kanban_replay_episode1'").fetchone():
        return []
    return [dict(row) for row in conn.execute(
        'SELECT o.*,e.reachable_ms FROM kanban_replay_episode1 e '
        'JOIN kanban_replay_observation1 o USING(board_incarnation,task,origin,evidence_sha256) '
        'WHERE e.reachable_ms>=300000 ORDER BY e.board_incarnation,e.task,e.origin')]
