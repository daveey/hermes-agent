"""Sender-side keyed diagnostic custody; delivery transport is integration-owned. [daveey]"""
import json
from uuid import uuid4

from hermes_cli.kanban_db_connect import write_txn

_SCHEMA = '''CREATE TABLE IF NOT EXISTS kanban_replay_diagnostic1 (
    board_incarnation TEXT NOT NULL, task TEXT NOT NULL, origin TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL, notice_key TEXT NOT NULL UNIQUE,
    owner_gateway TEXT, owner_profile TEXT, owner_chat TEXT, receipt_json TEXT,
    PRIMARY KEY(board_incarnation,task,origin)
)'''


def synchronize(conn, board, task):
    """Same transaction as current episode replacement; <=16 current keys per task.

    Resolved/superseded evidence is retired, never falsely marked delivered. This
    is bounded current-episode custody, not a permanent per-operation ledger.
    """
    if not conn.in_transaction:
        raise ValueError('diagnostic producer requires caller transaction')
    conn.execute(_SCHEMA)
    conn.execute('DELETE FROM kanban_replay_diagnostic1 AS d WHERE board_incarnation=? AND task=? '
                 'AND NOT EXISTS (SELECT 1 FROM kanban_replay_episode1 e '
                 'WHERE e.board_incarnation=d.board_incarnation AND e.task=d.task '
                 'AND e.origin=d.origin AND e.evidence_sha256=d.evidence_sha256)', (board, task))
    for row in conn.execute('SELECT origin,evidence_sha256 FROM kanban_replay_episode1 '
                            'WHERE board_incarnation=? AND task=? AND reachable_ms>=300000', (board, task)):
        conn.execute('INSERT OR IGNORE INTO kanban_replay_diagnostic1 '
                     '(board_incarnation,task,origin,evidence_sha256,notice_key) VALUES (?,?,?,?,?)',
                     (board, task, row['origin'], row['evidence_sha256'], 'replay-diagnostic-' + uuid4().hex))


def pending(conn):
    """Actionable, current undelivered episodes; a queue/sent flag is not a receipt."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_replay_diagnostic1'").fetchone():
        return []
    return [dict(row) for row in conn.execute(
        'SELECT d.*,o.hlc,o.gateway,o.run,o.status FROM kanban_replay_diagnostic1 d '
        'JOIN kanban_replay_episode1 e USING(board_incarnation,task,origin,evidence_sha256) '
        'JOIN kanban_replay_observation1 o USING(board_incarnation,task,origin,evidence_sha256) '
        'WHERE d.receipt_json IS NULL AND e.reachable_ms>=300000 ORDER BY d.board_incarnation,d.task,d.origin')]


def _read(row):
    return dict(id=row['notice_key'] + '-read', to_gw=row['owner_gateway'],
                profile=row['owner_profile'], chat=row['owner_chat'], keys=[row['notice_key']])


def bind_owner(conn, notice_key, *, gateway, profile, chat):
    """Integration supplies the existing authorized owner, never a fallback chat.

    Binding is immutable for this episode. No send, wake, business resolution or
    delivery claim occurs. The returned read uses the existing notice-receipts
    owner-transcript envelope and stays stable across retry/restart.
    """
    if any(not isinstance(s, str) or not s for s in (gateway, profile, chat)):
        raise ValueError('diagnostic owner identity required')
    with write_txn(conn):
        row = next((r for r in pending(conn) if r['notice_key'] == notice_key), None)
        if row is None:
            raise ValueError('diagnostic episode no longer pending')
        owner = (row['owner_gateway'], row['owner_profile'], row['owner_chat'])
        if owner != (None, None, None) and owner != (gateway, profile, chat):
            raise ValueError('diagnostic owner binding changed')
        conn.execute('UPDATE kanban_replay_diagnostic1 SET owner_gateway=?,owner_profile=?,owner_chat=? '
                     'WHERE notice_key=?', (gateway, profile, chat, notice_key))
    return _read(dict(row, owner_gateway=gateway, owner_profile=profile, owner_chat=chat))


def record_delivery(conn, notice_key):
    """Consume only the canonical stored owner-transcript readback, never a flag.

    Integration owns actual production/send/read queueing. Tests replace the
    canonical reader with fixtures; a fixture is not an actual owner delivery.
    """
    from hermes_cli import kanban_notify_reconcile_exec as notices
    row = next((r for r in pending(conn) if r['notice_key'] == notice_key), None)
    if row is None or row['owner_gateway'] is None:
        return False
    read = _read(row)
    sent = notices._sent(read['id'])
    if sent is None or sent['state'] != 'done':
        return False
    evidence = notices._evidence(read, sent)
    if not evidence['session_exists'] or notice_key not in evidence['delivered']:
        return False
    with write_txn(conn):
        current = next((r for r in pending(conn) if r['notice_key'] == notice_key), None)
        if current != row:
            return False
        conn.execute('UPDATE kanban_replay_diagnostic1 SET receipt_json=? WHERE notice_key=? AND receipt_json IS NULL',
                     (json.dumps(dict(read=read, evidence=evidence), sort_keys=True), notice_key))
    return True
