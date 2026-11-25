"""Recover published pre-rehome evidence skipped by the old primary partition.

Ownership of a task row does not imply custody of its prior home's history. Only
an identical rehome event held locally and in the replica admits this repair.
Missing historical events use negative ids, like pickup-prefix refill: no replay
through notification cursors, no capture, and no mutation of the current task.
Event consumers deciding spend use canonical timestamp/uid order, not these ids.
"""
from __future__ import annotations

import json
import sqlite3

from hermes_cli import kanban_db_sync as sync


def _boundary(conn, replica, tid, own):
    row = conn.execute('SELECT created_on FROM tasks WHERE id=?', (tid,)).fetchone()
    remote = replica.execute('SELECT created_on FROM tasks WHERE id=?', (tid,)).fetchone()
    if row is None or remote is None or row[0] != own or remote[0] != own:
        return None
    for event in conn.execute("SELECT uid,payload,origin_gateway FROM task_events WHERE task_id=? AND kind='rehomed' "
                              "ORDER BY created_at DESC,id DESC", (tid,)):
        try:
            payload = json.loads(event['payload'] or '{}')
        except (TypeError, ValueError):
            continue
        if (event['origin_gateway'] != 'server' or not isinstance(payload, dict) or payload.get('to') != own
                or not isinstance(payload.get('from'), str) or payload['from'] in ('', own)):
            continue
        match = replica.execute("SELECT id,payload FROM task_events WHERE task_id=? AND uid=? "
                                "AND kind='rehomed' AND origin_gateway='server'", (tid, event['uid'])).fetchone()
        if match is not None and match['payload'] == event['payload']:
            return match['id']
    return None


def recover(conn, replica, own, ids=None):
    """Missing pre-handoff events and their real runs, never task fields or release authority."""
    targets = list(ids) if ids is not None else [r[0] for r in conn.execute(
        "SELECT t.id FROM tasks t WHERE t.created_on=? AND t.status NOT IN ('done','archived') "
        "AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id=t.id AND e.kind='rehomed')", (own,))]
    report = {}
    for tid in targets:
        if _boundary(conn, replica, tid, own) is None:
            continue
        result = _recover_one(conn, replica, tid, own)
        if ids is not None or result['filled'] or result['unresolved']:
            report[tid] = result
    return report


def _missing(conn, replica, tid, boundary):
    held = {r[0] for r in conn.execute('SELECT uid FROM task_events WHERE task_id=?', (tid,))}
    # The boundary is Board publication order, not the sender's clock: only
    # evidence already published before the authenticated transfer is imported.
    return [r for r in replica.execute('SELECT * FROM task_events WHERE task_id=? AND id<? ORDER BY id',
                                      (tid, boundary)) if r['uid'] and r['uid'] not in held]


def _recover_one(conn, replica, tid, own):
    from hermes_cli import kanban_sync_client as client
    from hermes_cli.kanban_db_connect import write_txn

    filled = unresolved = 0
    boundary = _boundary(conn, replica, tid, own)
    if boundary is None or not _missing(conn, replica, tid, boundary):
        return {'filled': 0, 'unresolved': 0}
    with write_txn(conn):
        boundary = _boundary(conn, replica, tid, own)  # recheck custody under the writer lock
        if boundary is None or sync._tombstoned(conn, 'tasks', tid):
            return {'filled': 0, 'unresolved': 0}
        missing = _missing(conn, replica, tid, boundary)
        low = conn.execute('SELECT MIN(id) FROM task_events').fetchone()[0]
        next_id = min(0, low or 0) - 1
        applying = sync.sync_state(conn)['applying']
        conn.execute('UPDATE kanban_sync_state SET applying=1')
        try:
            for row in reversed(missing):
                uid = row['uid']
                if any(sync._tombstoned(db, 'task_events', uid) for db in (conn, replica)):
                    continue
                fields = client._row_fields(replica, 'task_events', row)
                run_uid = fields.get('run_uid')
                if ((row['run_id'] is not None and run_uid is None)
                        or (run_uid is not None and not _run(conn, replica, tid, run_uid))):
                    unresolved += 1
                    continue
                cols = sync._local_fields(conn, 'task_events', fields, tid)
                cols['origin_gateway'] = row['origin_gateway']
                names = ['id', 'uid', *cols]
                conn.execute(f"INSERT INTO task_events ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                             (next_id, uid, *cols.values()))
                next_id -= 1
                filled += 1
        finally:
            conn.execute('UPDATE kanban_sync_state SET applying=?', (applying,))
    return {'filled': filled, 'unresolved': unresolved}


def _run(conn, replica, tid, uid):
    """Fill only absent or untouched placeholder runs; never overwrite real local work."""
    from hermes_cli import kanban_sync_client as client

    if any(sync._tombstoned(db, 'task_runs', uid) for db in (conn, replica)):
        return False
    local = conn.execute('SELECT * FROM task_runs WHERE uid=?', (uid,)).fetchone()
    if local is not None and local['task_id'] != tid:
        return False
    if local is not None and local['status'] != 'pending_sync':
        return True
    remote = replica.execute('SELECT * FROM task_runs WHERE uid=? AND task_id=?', (uid, tid)).fetchone()
    if remote is None or remote['status'] == 'pending_sync':
        return False
    if conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='task_runs' AND row_uid=?", (uid,)).fetchone():
        return False
    fields = client._row_fields(replica, 'task_runs', remote)
    change = sync.RemoteChange(0, 'task_runs', uid, 'upsert', fields, int(remote['sync_version'] or 0), '', '')
    return sync._upsert_row(conn, change, '', sync.ApplyResult(cursor=0))
