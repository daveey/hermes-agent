"""Dormant W=1 replay journal in the caller's Board database. [daveey]

No production installer or transport calls this yet. Admission, capture provenance,
parent ordering and source-custody bookkeeping must be integrated before activation.
The caller owns the transaction and COMMIT, including source custody changes.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping

from hermes_cli.kanban_replay_reconcile import _identity, _local_state, reconcile
from hermes_cli.kanban_replay_wire import freeze, verify

_SCHEMA = """CREATE TABLE IF NOT EXISTS kanban_replay_sender1 (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    board_id TEXT NOT NULL, state TEXT NOT NULL, wire BLOB
)"""


def _transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction or getattr(conn, 'autocommit', False) is True:
        raise ValueError('caller transaction required')


def _reserve(conn: sqlite3.Connection) -> None:
    _transaction(conn)
    # Reserve before reading even when the caller used a deferred BEGIN.
    conn.execute('UPDATE kanban_replay_sender1 SET state=state WHERE singleton=1')


def _validate(state: dict) -> None:
    if not isinstance(state, dict):
        raise ValueError('invalid journal state')
    _local_state(state)
    for key in ('board_incarnation', 'gateway_principal', 'journal_id'):
        if not isinstance(state.get(key), str) or not state[key]:
            raise ValueError(f'invalid journal binding: {key}')


def _ack(state: dict) -> dict | None:
    return (dict(sequence=state['settled'], op_id=state['last_op_id'],
                 payload_sha256=state['last_payload_sha256']) if state['settled'] != '0' else None)


def _check_wire(state: dict, encoded: bytes | None) -> None:
    import rfc8785

    pending = state['pending']
    if pending is None:
        if encoded is not None:
            raise ValueError('unexpected pending wire')
        return
    if not isinstance(encoded, bytes):
        raise ValueError('missing pending wire')
    wire = json.loads(encoded)
    if not isinstance(wire, dict) or not isinstance(wire.get('replay'), dict):
        raise ValueError('invalid pending wire')
    replay = wire['replay']
    if (rfc8785.dumps(wire) != encoded or verify(wire) != pending['payload_sha256'] or
            wire.get('client_op_id') != pending['op_id'] or replay.get('protocol') != 'processed_prefix1' or
            replay.get('generation') != state['generation'] or replay.get('sequence') != pending['sequence'] or
            replay.get('ack') != _ack(state) or 'session' in replay):
        raise ValueError('pending wire custody mismatch')


def _read(conn: sqlite3.Connection) -> tuple[dict, bytes | None]:
    row = conn.execute('SELECT state,wire FROM kanban_replay_sender1 WHERE singleton=1').fetchone()
    if row is None:
        raise ValueError('missing journal state')
    state = json.loads(row[0])
    _validate(state)
    anchor = conn.execute('SELECT s.journal_id FROM kanban_sync_state s '
                          'JOIN kanban_replay_sender1 j ON s.board_id=j.board_id '
                          'WHERE j.singleton=1').fetchone()
    if anchor is None or anchor[0] != state['journal_id']:
        raise ValueError('sync journal binding mismatch')
    _check_wire(state, row[1])
    return state, row[1]


def initialize(conn: sqlite3.Connection, binding: dict, *, board_id: str) -> None:
    """Bind a first journal atomically; never reset one after loss.

    The caller must obtain the serving Board's authenticated enrollment first.
    This local binding is not remote enrollment or migration readiness.
    """
    _transaction(conn)
    _validate(binding)
    if binding['settled'] != '0' or binding['pending'] is not None:
        raise ValueError('initial journal must be empty')
    conn.execute('SAVEPOINT replay_initialize1')
    try:
        updated = conn.execute('UPDATE kanban_sync_state SET journal_id=? '
                               'WHERE board_id=? AND journal_id IS NULL',
                               (binding['journal_id'], board_id))
        if updated.rowcount != 1:
            raise ValueError('journal already bound; recovery required')
        conn.execute(_SCHEMA)
        conn.execute('INSERT INTO kanban_replay_sender1(singleton,board_id,state) VALUES (1,?,?)',
                     (board_id, json.dumps(binding)))
        conn.execute('RELEASE replay_initialize1')
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK TO replay_initialize1')
            conn.execute('RELEASE replay_initialize1')
        raise


def snapshot(conn: sqlite3.Connection) -> dict:
    """Read and validate persisted allocation/custody without business-row hydration."""
    return _read(conn)[0]


def _bound_custody(conn: sqlite3.Connection) -> bool:
    """A lost anchor cannot erase surviving enrollment or recovery-loss evidence."""
    anchor = conn.execute('SELECT journal_id,replay_restore_required FROM kanban_sync_state LIMIT 1').fetchone()
    if anchor is not None and anchor[0] is not None:
        return True
    evidence = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN "
                            "('kanban_replay_sender1','kanban_replay_recovery_loss1') LIMIT 1").fetchone()
    if evidence or (anchor is not None and anchor[1] != 0):
        raise ValueError('journal restore reconciliation required; custody LOSS or invalid anchor')
    return False


def bound_custody(conn: sqlite3.Connection) -> bool:
    """True only for a live bound journal.

    Missing, NULL or recreated anchors with surviving journal, wire or loss
    evidence raise instead of looking never-enrolled.
    """
    return _bound_custody(conn)


def guard_import(conn: sqlite3.Connection, tables: tuple[str, ...], only: Mapping[str, set[str]] | None) -> bool:
    """Fence import scopes under the writer reservation; imports are not replay repair."""
    _transaction(conn)
    if not _bound_custody(conn):
        return False
    _reserve(conn)
    _state, encoded = _read(conn)
    held = list(conn.execute("SELECT table_name,row_uid FROM kanban_sync_outbox WHERE state='rejected'"))
    if encoded is not None:
        wire = json.loads(encoded)
        from hermes_cli.kanban_replay_controls import routing
        held.append(routing(wire))
    for table, uid in held:
        if table in tables and (only is None or uid in only.get(table, set())):
            raise ValueError('import overlaps replay custody; explicit reconciliation required')
    return True


def issued_operation(conn: sqlite3.Connection) -> str | None:
    """Reserve and validate bound custody before an explicit identity-changing repair."""
    if not _bound_custody(conn):
        return None
    _reserve(conn)
    state, _encoded = _read(conn)
    return state['pending']['op_id'] if state['pending'] is not None else None


def parent_cleanup_hold(conn: sqlite3.Connection, *, task_id: str | None = None,
                        remote: dict | None = None) -> str | None:
    """Keep an issued task cache until its authenticated exact final settlement.

    The sender supplies its inspected serving-Board slot only inside recover's
    bookkeeping transaction. Ordinary cleanup has no authority to infer a final
    outcome from local row existence or a newer sync version.
    """
    if not _bound_custody(conn):
        return None
    _reserve(conn)
    state, encoded = _read(conn)
    if encoded is None:
        return None
    wire = json.loads(encoded)
    if wire.get('table') != 'tasks':
        return None
    held = wire['uid']
    if task_id == held:
        if remote is None or reconcile(state, remote)['action'] != 'record':
            raise ValueError('issued parent requires final verdict')
        saved = conn.execute('SELECT wire FROM kanban_sync_parent_envelopes WHERE task_id=?', (held,)).fetchone()
        del wire['replay']
        if saved is None or json.loads(saved[0]) != wire:
            raise ValueError('issued parent cache custody mismatch')
        return None
    return held


def pending_wire(conn: sqlite3.Connection) -> bytes | None:
    """Return exact canonical bytes after validating identity, digest and ack custody."""
    return _read(conn)[1]


def issue(conn: sqlite3.Connection, intent: dict, *, source_op: str | None = None) -> bytes:
    """Freeze only the dependency-ordered head selected by the serialized sender."""
    import rfc8785

    _reserve(conn)
    state = snapshot(conn)
    restore = conn.execute('SELECT replay_restore_required FROM kanban_sync_state '
                           'WHERE board_id=(SELECT board_id FROM kanban_replay_sender1 WHERE singleton=1)').fetchone()
    if restore[0] != 0:
        raise ValueError('journal restore reconciliation required')
    if state['pending'] is not None:
        raise ValueError('outstanding issued envelope')
    if 'replay' in intent:
        raise ValueError('intent already framed')
    _identity(intent.get('client_op_id'), '0' * 64, 'intent')
    sequence = str(int(state['settled']) + 1)
    wire = dict(intent)
    replay: dict = dict(protocol='processed_prefix1', generation=state['generation'], sequence=sequence)
    ack = _ack(state)
    if ack is not None:
        replay['ack'] = ack
    wire['replay'] = replay
    wire = freeze(wire)
    state['pending'] = dict(sequence=sequence, op_id=wire['client_op_id'],
                            payload_sha256=wire['replay']['payload_sha256'])
    if source_op is not None:
        state['pending']['source_op'] = source_op
    _validate(state)
    encoded = rfc8785.dumps(wire)
    conn.execute('UPDATE kanban_replay_sender1 SET state=?,wire=? WHERE singleton=1',
                 (json.dumps(state), encoded))
    return encoded


def recover(conn: sqlite3.Connection, remote: dict, record: Callable[[dict], None]) -> dict:
    """Record an inspected exact final verdict with caller's bookkeeping/prune.

    ``record`` must persist visible rejection/cancellation/deferred custody or
    success bookkeeping in this transaction. It must not commit or issue work.
    Authentication and serving capability are the transport caller's obligations.
    """
    _reserve(conn)
    state = snapshot(conn)
    decision = reconcile(state, remote)
    if decision['action'] == 'record':
        conn.execute('SAVEPOINT replay_record1')
        try:
            record(decision['verdict'])
            pending = state['pending']
            state.update(settled=pending['sequence'], last_op_id=pending['op_id'],
                         last_payload_sha256=pending['payload_sha256'],
                         last_verdict=remote['last_verdict'], pending=None)
            conn.execute('UPDATE kanban_replay_sender1 SET state=?,wire=NULL WHERE singleton=1',
                         (json.dumps(state),))
            conn.execute('RELEASE replay_record1')
        except BaseException:
            # Storage faults may already have rolled back the outer transaction.
            if conn.in_transaction:
                conn.execute('ROLLBACK TO replay_record1')
                conn.execute('RELEASE replay_record1')
            raise
    return decision


def mark_restore_required(conn: sqlite3.Connection) -> None:
    """Recovery tooling marks a copied database before any sender may allocate."""
    _reserve(conn)
    snapshot(conn)
    conn.execute('UPDATE kanban_sync_state SET replay_restore_required=1 '
                 'WHERE board_id=(SELECT board_id FROM kanban_replay_sender1 WHERE singleton=1)')


def reconcile_restore(conn: sqlite3.Connection, remote: dict, record: Callable[[dict], None]) -> dict:
    """Clear the restore hold only after exact byte/verdict reconciliation.

    Caller authenticates the serving Board and current session, and owns COMMIT.
    An incompatible copy remains held; this does not rotate a lost journal.
    """
    decision = recover(conn, remote, record)
    conn.execute('UPDATE kanban_sync_state SET replay_restore_required=0 '
                 'WHERE board_id=(SELECT board_id FROM kanban_replay_sender1 WHERE singleton=1)')
    return decision
