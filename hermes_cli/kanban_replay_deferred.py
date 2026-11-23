"""Active deferred task/origin handoffs, separate from row success. [daveey]

This bounded custody is not current remote queue membership. Observation and
five-minute diagnostic delivery remain staged; no resolver may infer application
from disappearance here. No permanent operation-result ledger is retained.
"""


_SCHEMA = '''CREATE TABLE IF NOT EXISTS kanban_replay_deferred1 (
    board_incarnation TEXT NOT NULL, task TEXT NOT NULL, origin TEXT NOT NULL,
    status TEXT NOT NULL, hlc TEXT NOT NULL,
    PRIMARY KEY(board_incarnation, task, origin)
)'''


def handoff(conn, wire, verdict, board_incarnation):
    """Retain the exact issued reference inside the journal's settlement transaction."""
    if not conn.in_transaction:
        raise ValueError('deferred custody transaction required')
    reference = verdict.get('deferred')
    if (wire.get('table') != 'tasks' or wire.get('op') != 'upsert' or
            not isinstance(wire.get('status_origin'), str) or not wire['status_origin'] or
            not isinstance(wire.get('fields', {}).get('status'), str) or
            reference != dict(task=wire['uid'], origin=wire['status_origin'])):
        raise ValueError('deferred reference does not match issued status')
    conn.execute(_SCHEMA)
    key = (board_incarnation, wire['uid'], wire['status_origin'])
    exists = conn.execute('SELECT 1 FROM kanban_replay_deferred1 '
                          'WHERE board_incarnation=? AND task=? AND origin=?', key).fetchone()
    count = conn.execute('SELECT COUNT(*) FROM kanban_replay_deferred1 '
                         'WHERE board_incarnation=? AND task=?', key[:2]).fetchone()[0]
    if not exists and count >= 16:
        raise ValueError('deferred custody requires fresh membership reconciliation')
    conn.execute('INSERT INTO kanban_replay_deferred1 VALUES (?,?,?,?,?) '
                 'ON CONFLICT(board_incarnation,task,origin) DO UPDATE SET '
                 'status=excluded.status,hlc=excluded.hlc',
                 (*key, wire['fields']['status'], wire['hlc']))


def pending(conn):
    """Read visible handoff references; these are not applied/current-row results."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='kanban_replay_deferred1'").fetchone():
        return []
    return [dict(zip(('board_incarnation', 'task', 'origin', 'status', 'hlc'), row))
            for row in conn.execute('SELECT board_incarnation,task,origin,status,hlc '
                                    'FROM kanban_replay_deferred1 ORDER BY board_incarnation,task,origin')]
