"""Bounded read-only local inputs for a current historical audit, not a hold release.

Containment/spend restrictions remain untouched and are not vetoed merely for
existing: this classification authorizes no execution. Quarantine, omissions,
new local work and any active executor do veto it. [daveey]
"""
from __future__ import annotations

import json

from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_history_residue_source import _positive, _require, decode_source, strict_object

MAX_ROWS = 100
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024


def _rows(conn, sql, args=()):
    cursor = conn.execute(sql, args)
    # Check encoded byte budget BEFORE materializing potentially large source cells.
    names = [c[0] for c in cursor.description]
    bound = '+'.join(f'6*COALESCE(length(CAST("{c}" AS BLOB)),4)' for c in names)
    total = conn.execute(f'SELECT COALESCE(sum({bound}),0),count(*) FROM ({sql})', args).fetchone()
    _require(total[0] <= MAX_SNAPSHOT_BYTES and total[1] <= MAX_ROWS, 'local_snapshot_bound')
    result = []
    size = 0
    for row in cursor.fetchmany(MAX_ROWS + 1):
        _require(len(result) < MAX_ROWS, 'local_rows_bound')
        item = dict(zip(names, row))
        size += len(json.dumps(item, ensure_ascii=True).encode())
        _require(size <= MAX_SNAPSHOT_BYTES, 'local_bytes_bound')
        result.append(item)
    return result


def _portable(task):
    columns = sync.SYNCED_COLUMNS['tasks'] | sync.SERVER_COLUMNS['tasks']
    _require(columns <= task.keys(), 'task_schema_incomplete')
    out = {k: task[k] for k in sorted(columns - {'current_run_id'})}
    for key in sync.JSON_SERVER_COLUMNS['tasks']:
        if isinstance(out[key], str):
            out[key] = strict_object(out[key], max_bytes=MAX_SNAPSHOT_BYTES)
    out.update(id=task['id'], uid=task['id'], version=task['sync_version'], current_run_uid=None)
    return out


def snapshot(conn, exact_source, *, board, gateway, replica):
    """Caller pins a read transaction; final cut rechecks under short writer reservations.

    Missing schema/errors fail closed. Return complete relevant rows for equality
    comparison; do not normalize source fields, frozen wire or local history IDs.
    """
    source = decode_source(exact_source)
    fields = strict_object(source['fields'])
    task_id, event = fields['task_id'], source['row_uid']
    states = _rows(conn, 'SELECT * FROM kanban_sync_state')
    _require(len(states) == 1 and states[0]['board_id'] == board and states[0]['gateway_id'] == gateway
             and states[0]['mode'] == ('shadow' if replica else 'primary')
             and states[0]['applying'] == 0, 'local_sync_identity')
    tasks = _rows(conn, 'SELECT * FROM tasks WHERE id=?', (task_id,))
    _require(len(tasks) == 1, 'local_task_missing')
    task = tasks[0]
    _require(task['status'] in ('triage', 'archived') and task['created_on'] == gateway
             and _positive(task['sync_version']), 'local_task_identity')
    for key in ('current_run_id', 'claim_lock', 'claim_expires', 'worker_pid', 'worker_started_at',
                'lease_holder', 'lease_expires'):
        _require(key in task and task[key] is None, 'local_executor_active')
    runs = _rows(conn, 'SELECT * FROM task_runs WHERE task_id=? ORDER BY id', (task_id,))
    _require(all(r['ended_at'] is not None and r['status'] != 'running' for r in runs), 'local_running_run')
    # A run DELETE carries fields={} after its row disappears. Its parent cannot
    # be recovered from the remaining UID: fail closed on unresolved orphan
    # work anywhere on this board, not on settled historical tombstones alone.
    orphan_ops = _rows(conn, "SELECT * FROM kanban_sync_outbox o WHERE table_name='task_runs' "
                       "AND NOT EXISTS (SELECT 1 FROM task_runs r WHERE r.uid=o.row_uid) ORDER BY op_id")
    orphan_omissions = _rows(conn, "SELECT * FROM kanban_sync_omissions o WHERE table_name='task_runs' "
                             "AND state!='delivered' AND NOT EXISTS "
                             "(SELECT 1 FROM task_runs r WHERE r.uid=o.row_uid) ORDER BY row_uid,field")
    _require(not orphan_ops and not orphan_omissions, 'unattributed_run_work')
    events = _rows(conn, 'SELECT * FROM task_events WHERE uid=? ORDER BY id', (event,))
    if replica:
        _require(not events, 'replica_event_present')
    else:
        _require(len(events) == 1, 'local_event_missing')
        e = events[0]
        actual = {key: e[key] for key in ('task_id', 'kind', 'payload', 'created_at')}
        actual['run_uid'] = None
        _require(e['run_id'] is None and actual == fields, 'local_event_changed')
    ops = _rows(conn, "SELECT * FROM kanban_sync_outbox WHERE (table_name='tasks' AND row_uid=?) "
                "OR (table_name='task_events' AND row_uid=?) OR "
                "(table_name='task_runs' AND row_uid IN (SELECT uid FROM task_runs WHERE task_id=?)) OR "
                "CASE WHEN json_valid(fields) THEN json_extract(fields,'$.task_id')=? ELSE 0 END ORDER BY op_id",
                (task_id, event, task_id, task_id))
    if replica:
        _require(not ops, 'replica_local_work')
    else:
        same = [o for o in ops if o['row_uid'] == event and o['table_name'] == 'task_events']
        _require(len(same) == 1 and json.dumps(same[0], sort_keys=True, ensure_ascii=True) ==
                 json.dumps(source, sort_keys=True, ensure_ascii=True), 'local_source_changed')
        _require(all(o['table_name'] == 'task_events' and o['state'] == 'rejected' for o in ops), 'local_uncovered_work')
    tombstones = _rows(conn, 'SELECT * FROM kanban_sync_tombstones WHERE '
                       "(table_name='tasks' AND row_uid=?) OR (table_name='task_events' AND row_uid=?) OR "
                       "(table_name='task_runs' AND row_uid IN (SELECT uid FROM task_runs WHERE task_id=?))",
                       (task_id, event, task_id))
    omissions = _rows(conn, "SELECT * FROM kanban_sync_omissions WHERE state!='delivered' AND "
                     "((table_name='tasks' AND row_uid=?) OR (table_name='task_events' AND row_uid=?) OR "
                     "(table_name='task_runs' AND row_uid IN (SELECT uid FROM task_runs WHERE task_id=?)))",
                     (task_id, event, task_id))
    _require(not tombstones and not omissions, 'local_repair_hold')
    from hermes_cli.kanban_sync_quarantine import held
    _require(not held(conn, task_id), 'local_quarantine')
    return {'state': states, 'task': tasks, 'portable': _portable(task), 'runs': runs,
            'event': events, 'ops': ops, 'tombstones': tombstones, 'omissions': omissions}
