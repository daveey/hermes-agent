"""Keep ordinary creator handoffs from revoking queued origin admission.

Reservations are obligations, never authority. Only the origin can authorize a
forward. Mandatory stops and kernel cleanup must remain possible; those exits
cannot promise delivery of still-unadmitted requests.
"""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3


VOLUNTARY = frozenset({'continued', 'scheduled', 'completed', 'blocked',
                       'review_requested', 'changes_requested'})
_RESERVED = 'origin_forward_reserved'


def reserve(conn, task_id, run_uid, request_id):
    """Commit an obligation under the same writer lock as ordinary run endings.

    Called before starting the courier subprocess. A concurrent ending either
    sees this reservation or wins first, in which case nothing is submitted.
    """
    from hermes_cli import kanban_db as kb
    if conn.in_transaction:
        raise ValueError('origin_forward_submit_transaction: reservation must commit before enqueue')
    with kb.write_txn(conn):
        row = conn.execute('SELECT r.id FROM tasks t JOIN task_runs r ON r.id=t.current_run_id '
                           "WHERE t.id=? AND t.status='running' AND r.task_id=t.id "
                           'AND r.uid=? AND r.ended_at IS NULL', (task_id, run_uid)).fetchone()
        if row is None:
            raise ValueError('origin_forward_submit_run_ended: no request submitted')
        found = conn.execute('SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND kind=? '
                             "AND json_extract(payload,'$.request_id')=?",
                             (task_id, row[0], _RESERVED, request_id)).fetchone()
        if not found:
            kb._append_event(conn, task_id, _RESERVED, {'request_id': request_id}, run_id=row[0])


def _pending(identity):
    raise ValueError('origin_forward_pending: keep this run alive until request ' + str(identity)
                     + ' is admitted or definitively rejected. Fetch its courier outcome and retry '
                     'this lifecycle call; do not recreate or modify the frozen request.')


def require_settled(conn, task_id, run_id, outcome):
    """Refuse ordinary endings transactionally; never interfere with mandatory stops."""
    from hermes_cli.kanban_workspace_publication import _kernel_park
    if outcome not in VOLUNTARY or _kernel_park.get() == task_id:
        return
    from hermes_cli import kanban_db_sync as sync
    if 'uid' not in sync.table_columns(conn, 'task_runs'):
        return
    row = conn.execute('SELECT uid FROM task_runs WHERE id=? AND task_id=?', (run_id, task_id)).fetchone()
    if not row or not row[0]:
        return
    reservations = {json.loads(event[0])['request_id'] for event in conn.execute(
        'SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind=?', (task_id, run_id, _RESERVED))}
    from hermes_cli.kanban_capability_registry import hub_db_path
    path = hub_db_path()
    try:
        path.lstat()
    except FileNotFoundError:
        if reservations:
            _pending(sorted(reservations)[0])
        return
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            for identity in sorted(reservations):
                sent = db.execute('SELECT state FROM sent WHERE id=?', (identity,)).fetchone()
                if not sent or sent[0] not in ('done', 'rejected'):
                    _pending(identity)
            # Also protect requests already queued by pre-reservation core.
            rows = db.execute("SELECT id,body FROM sent WHERE (state IS NULL OR state NOT IN ('done','rejected')) "
                              "AND body LIKE '%origin-forward%'")
            for identity, raw in rows:
                request = json.loads(raw)
                args = request.get('args', {})
                if (request.get('coll') == 'sessions' and request.get('action') == 'origin-forward'
                        and args.get('run_uid') == row[0] and args.get('creator', {}).get('task_id') == task_id):
                    _pending(identity)
    except (sqlite3.Error, OSError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError('origin_forward_evidence_unavailable: cannot verify outstanding forwards; '
                         'keep this run alive and retry the courier read') from exc
