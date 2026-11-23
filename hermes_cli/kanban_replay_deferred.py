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


def handoff(conn, wire, verdict, board_incarnation, *, membership=None):
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
    if membership is not None:
        origins, observed = membership
        # Retire only the pre-read capture: a newer handoff during I/O is not
        # evidence covered by that observation, even when it reuses an origin.
        for row in observed:
            if row['origin'] not in origins:
                conn.execute('DELETE FROM kanban_replay_deferred1 '
                             'WHERE board_incarnation=? AND task=? AND origin=? AND status=? AND hlc=?',
                             (*key[:2], row['origin'], row['status'], row['hlc']))
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


def validate_membership(reply, local, task):
    """Validate a complete future-contract read before retiring any local reference."""
    import re
    if not isinstance(reply, dict):
        raise ValueError('invalid deferred observation object')
    expected = dict(protocol='origin_pending1', board_incarnation=local['board_incarnation'],
                    gateway_principal=local['gateway_principal'], task=task)
    for key, value in expected.items():
        if reply.get(key) != value:
            raise ValueError(f'invalid deferred observation {key}')
    if reply.get('complete') is not True:
        raise ValueError('invalid deferred observation completeness')
    entries = reply.get('entries')
    if not isinstance(entries, list) or len(entries) > 16:
        raise ValueError('invalid deferred observation entries')
    origins = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('invalid deferred observation entry')
        for key in ('origin', 'hlc', 'gateway', 'status'):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise ValueError(f'invalid deferred observation {key}')
        if 'run' not in entry or (entry['run'] is not None and
                                  (not isinstance(entry['run'], str) or not entry['run'])):
            raise ValueError('invalid deferred observation run')
        digest = entry.get('evidence_sha256')
        if not isinstance(digest, str) or re.fullmatch('[a-f0-9]{64}', digest) is None:
            raise ValueError('invalid deferred observation fingerprint')
        if entry['origin'] in origins:
            raise ValueError('invalid deferred observation duplicate origin')
        origins.add(entry['origin'])
    return origins


def pending(conn):
    """Read visible handoff references; these are not applied/current-row results."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='kanban_replay_deferred1'").fetchone():
        return []
    return [dict(zip(('board_incarnation', 'task', 'origin', 'status', 'hlc'), row))
            for row in conn.execute('SELECT board_incarnation,task,origin,status,hlc '
                                    'FROM kanban_replay_deferred1 ORDER BY board_incarnation,task,origin')]
