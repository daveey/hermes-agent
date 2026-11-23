"""Bounded current-image recovery for one W=1 stream, not a result ledger. [daveey]"""
import json
import logging
from time import time

from hermes_cli import kanban_replay_journal as journal
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_sync_client import SyncUnavailable

_SCHEMA = '''CREATE TABLE IF NOT EXISTS kanban_replay_existence_fault1 (
    id INTEGER PRIMARY KEY CHECK(id=1), journal_id TEXT NOT NULL,
    generation TEXT NOT NULL, sequence TEXT NOT NULL, op_id TEXT NOT NULL,
    task TEXT NOT NULL, reason TEXT NOT NULL, recovery TEXT NOT NULL,
    attempts INTEGER NOT NULL, retry_after REAL NOT NULL, paused INTEGER NOT NULL
)'''


class CurrentRowHold(ValueError, SyncUnavailable):
    """A current-image read cannot safely settle the retained source."""
    def __init__(self, reason, *, transient=False):
        super().__init__(reason)
        self.transient = transient
        self.expected = None


def fault(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_replay_existence_fault1'").fetchone():
        return None
    row = conn.execute('SELECT * FROM kanban_replay_existence_fault1 WHERE id=1').fetchone()
    return dict(row) if row else None


def guard(conn):
    held = fault(conn)
    if held and (held['paused'] or time() < held['retry_after']):
        raise CurrentRowHold(f"Replay current-row hold: {held['reason']}; {held['recovery']}")


def pause(conn, board, error):
    """Publish after rollback. Network failures retry <=8 times with <=60s backoff."""
    with write_txn(conn):
        state = journal.snapshot(conn)
        pending = state['pending']
        if pending is None:
            raise SyncUnavailable('current-row recovery was superseded; inspect the settled journal')
        identity = (state['journal_id'], state['generation'], pending['sequence'], pending['op_id'])
        if error.expected is not None and error.expected != identity:
            raise SyncUnavailable('current-row recovery was superseded; preserve the successor custody')
        wire = json.loads(journal.pending_wire(conn))
        recovery = (f'Inspect serving Board current state and reconcile its prerequisite; then '
                    f'hermes kanban --board {board} sync replay-resume --expect {pending["op_id"]} '
                    '--reason "owner verified prerequisite". Retry exact custody; never skip or cancel.')
        conn.execute(_SCHEMA)
        old = fault(conn)
        if old and (old['op_id'], old['journal_id'], old['generation'], old['sequence']) != (
                pending['op_id'], state['journal_id'], state['generation'], pending['sequence']):
            raise CurrentRowHold('recovery fault binding changed; reconcile saved journal before resuming')
        attempts = min(8, old['attempts'] + 1) if old else 1
        paused = not error.transient or attempts >= 8
        notify = paused and not (old and old['paused'])
        conn.execute('INSERT INTO kanban_replay_existence_fault1 VALUES (1,?,?,?,?,?,?,?,?,?,?) '
                     'ON CONFLICT(id) DO UPDATE SET reason=excluded.reason,attempts=excluded.attempts,'
                     'retry_after=excluded.retry_after,paused=excluded.paused',
                     (state['journal_id'], state['generation'], pending['sequence'], pending['op_id'],
                      wire.get('uid', wire.get('task')), str(error)[:500], recovery, attempts, time() + min(60, 2 ** attempts), int(paused)))
    if notify:
        logging.getLogger(__name__).error('Replay current-row hold: %s', fault(conn))
    guard(conn)


def clear_settled(conn):
    """Only an exact locally recorded final verdict can retire a retry episode."""
    held = fault(conn)
    if held is None:
        return
    state = journal.snapshot(conn)
    if (state['pending'] is None and state['journal_id'] == held['journal_id'] and
            state['generation'] == held['generation'] and state['settled'] == held['sequence'] and
            state['last_op_id'] == held['op_id']):
        conn.execute('DELETE FROM kanban_replay_existence_fault1 WHERE id=1')


def resume(conn, expected_op, reason):
    """Explicit retry clears only a fault still bound to the exact pending slot."""
    if not reason.strip():
        raise ValueError('owner recovery reason required')
    with write_txn(conn):
        held = fault(conn)
        state = journal.snapshot(conn)
        pending = state['pending']
        if (held is None or pending is None or expected_op != held['op_id'] or
                pending['op_id'] != held['op_id'] or pending['sequence'] != held['sequence'] or
                state['journal_id'] != held['journal_id'] or state['generation'] != held['generation']):
            raise ValueError('current-row recovery identity changed; inspect without discarding custody')
        conn.execute('DELETE FROM kanban_replay_existence_fault1 WHERE id=1')
    return dict(action='retry_exact', op_id=expected_op, reason=reason[:500])
