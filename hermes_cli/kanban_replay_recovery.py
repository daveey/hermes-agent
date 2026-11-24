"""Durable bounded recovery for one strict stream; never a result ledger. [daveey]"""
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic as time
from uuid import uuid4

from hermes_cli import kanban_replay_journal as journal
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli import kanban_sync_client as client

_SCHEMA = '''CREATE TABLE IF NOT EXISTS kanban_replay_transport_fault1 (
    id INTEGER PRIMARY KEY CHECK(id=1), journal_id TEXT NOT NULL,
    generation TEXT NOT NULL, sequence TEXT NOT NULL, op_id TEXT NOT NULL,
    attempts INTEGER NOT NULL, reachable_s REAL NOT NULL, retry_after REAL NOT NULL,
    paused INTEGER NOT NULL, reason TEXT NOT NULL, recovery TEXT NOT NULL,
    retry_clock TEXT NOT NULL DEFAULT ''
)'''
_SAMPLE = ContextVar('replay_transport_recovery', default=None)
_CLOCK = uuid4().hex


def _schema(conn):
    conn.execute(_SCHEMA)
    if 'retry_clock' not in {row[1] for row in conn.execute('PRAGMA table_info(kanban_replay_transport_fault1)')}:
        conn.execute("ALTER TABLE kanban_replay_transport_fault1 ADD COLUMN retry_clock TEXT NOT NULL DEFAULT ''")


def _deadline(conn, held):
    if held.get('retry_clock') == _CLOCK:
        return held['retry_after']
    # Persisted monotonic values are meaningful only to their clock owner.
    # Restart/reboot/host transfer rearms one bounded delay, never elapsed age
    # or attempts. Old wall-clock rows take the same conservative migration.
    with write_txn(conn):
        current = fault(conn)
        if current != held or _bound(held) != _identity(journal.snapshot(conn)):
            raise client.SyncUnavailable('replay recovery changed while reanchoring clock')
        _schema(conn)
        deadline = time() + min(60, 2 ** held['attempts'])
        conn.execute('UPDATE kanban_replay_transport_fault1 SET retry_after=?,retry_clock=? WHERE id=1',
                     (deadline, _CLOCK))
    return deadline


def fault(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_replay_transport_fault1'").fetchone():
        return None
    row = conn.execute('SELECT * FROM kanban_replay_transport_fault1 WHERE id=1').fetchone()
    return dict(row) if row else None


def _identity(state):
    pending = state['pending']
    return (state['journal_id'], state['generation'],
            pending['sequence'] if pending else state['settled'],
            pending['op_id'] if pending else state['last_op_id'] or state['journal_id'])


def _bound(held):
    return tuple(held[key] for key in ('journal_id', 'generation', 'sequence', 'op_id'))


def guard(conn, *, mutation=False):
    held = fault(conn)
    if held is None:
        return
    if _bound(held) != _identity(journal.snapshot(conn)):
        raise client.SyncUnavailable('replay recovery identity changed; inspect without discarding custody')
    if held['paused']:
        raise client.SyncUnavailable(held['recovery'])
    if mutation and time() < _deadline(conn, held):
        raise client.SyncUnavailable('replay recovery backoff; ' + held['recovery'])


def reachable():
    """Start only after a successful authenticated exchange in this attempt.

    No baseline crosses a public turn, process restart or wholly offline attempt.
    Failed connect/probe time before the first successful exchange is uncharged.
    """
    sample = _SAMPLE.get()
    if sample is not None and sample['started'] is None:
        sample['started'] = time()


def call(hub, method, path, body=None):
    result = hub._call(method, path, body) if body is not None else hub._call(method, path)
    reachable()
    return result


def _failed(conn, board, error, started):
    if started is None:
        return  # Offline custody is unchanged, not an aging/retry-budget event.
    with write_txn(conn):
        state = journal.snapshot(conn)
        identity = _identity(state)
        old = fault(conn)
        if old is not None and _bound(old) != identity:
            # Earlier work in this turn may already have committed. Only exact
            # successor custody starts a new recovery episode, never old bytes.
            if old['journal_id'] != identity[0] or old['generation'] != identity[1]:
                raise client.SyncUnavailable('replay recovery identity changed; inspect saved journal')
            old = None
        attempts = min(8, old['attempts'] + 1) if old else 1
        elapsed = min(300.0, (old['reachable_s'] if old else 0.0) + max(0.0, time() - started))
        paused = attempts >= 8 or elapsed >= 300
        recovery = (f'Replay journal {identity[0]} sequence {identity[2]} stopped or backing off: '
                    f'{str(error)[:500]}. Inspect serving Board and repair transport; then '
                    f'hermes kanban --board {board} sync replay-resume --expect {identity[3]} '
                    '--reason "owner verified repair". Preserve exact custody; never skip or cancel.')
        _schema(conn)
        conn.execute('INSERT OR REPLACE INTO kanban_replay_transport_fault1 VALUES (1,?,?,?,?,?,?,?,?,?,?,?)',
                     (*identity, attempts, elapsed, time() + min(60, 2 ** attempts), int(paused),
                      str(error)[:500], recovery, _CLOCK))
    if paused and not (old and old['paused']):
        logging.getLogger(__name__).error('%s', recovery)


def completed(conn):
    state = journal.snapshot(conn)
    if state['pending'] is None:
        # A successful predecessor retires its elapsed window even when it never
        # created a durable fault. The next issued identity owns only its time.
        sample = _SAMPLE.get()
        if sample is not None and sample['started'] is not None:
            sample['started'] = time()
    held = fault(conn)
    if held is None:
        return
    with write_txn(conn):
        state = journal.snapshot(conn)
        if (state['journal_id'] == held['journal_id'] and state['generation'] == held['generation']
                and state['pending'] is None and int(state['settled']) >= int(held['sequence'])):
            conn.execute('DELETE FROM kanban_replay_transport_fault1 WHERE id=1')


@contextmanager
def turn(conn, board):
    guard(conn)  # Refuse before any capability/session I/O and outside failure accounting.
    sample = {'started': None}
    token = _SAMPLE.set(sample)
    try:
        yield
    except (OSError, client.HubError) as exc:
        _failed(conn, board, exc, sample['started'])
        raise
    else:
        completed(conn)
    finally:
        _SAMPLE.reset(token)


def resume(conn, expected_op, reason):
    if not reason.strip():
        raise ValueError('owner recovery reason required')
    with write_txn(conn):
        held = fault(conn)
        if held is None or expected_op != held['op_id'] or _bound(held) != _identity(journal.snapshot(conn)):
            raise ValueError('transport recovery identity changed; inspect without discarding custody')
        conn.execute('DELETE FROM kanban_replay_transport_fault1 WHERE id=1')
    return dict(action='retry_exact', op_id=expected_op, reason=reason[:500])
