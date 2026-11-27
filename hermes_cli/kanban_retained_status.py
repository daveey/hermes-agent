"""Read-only stopped-scope presentation evidence; never release or completion authority."""
from __future__ import annotations

import json
import sqlite3

from hermes_cli.kanban_sync_divergence import _event_order, _LEFT_PARK

_RELEVANT = _LEFT_PARK | {'decision_retained', 'scope_renewed', 'claimed', 'scheduled', 'continued',
                        'blocked', 'review_requested'}


def annotate(conn, rows):
    """Capture from the same board snapshot, including rehomed SQL replicas.

    Missing schema/history leaves ordinary triage untouched. Any later execution,
    renewal or park invalidates this display-only receipt, even if status stayed triage.
    """
    targets = {str(row['id']) for row in rows if row.get('status') == 'triage'}
    if not targets:
        return
    try:
        tasks = {r[0]: r[1:] for r in conn.execute(
            'SELECT id,current_run_id,claim_lock,claim_expires,human_request, '
            "CASE WHEN EXISTS (SELECT 1 FROM task_runs r WHERE r.task_id=tasks.id "
            "AND r.status='running' AND r.ended_at IS NULL) THEN 1 ELSE NULL END FROM tasks "
            'WHERE id IN (SELECT value FROM json_each(?))', (json.dumps(sorted(targets)),))}
        cursor = conn.execute(
            'SELECT * FROM task_events WHERE task_id IN '
            '(SELECT value FROM json_each(?)) AND kind IN (SELECT value FROM json_each(?))',
            (json.dumps(sorted(targets)), json.dumps(sorted(_RELEVANT))))
        columns = [column[0] for column in cursor.description]
        events = [dict(zip(columns, row)) for row in cursor.fetchall()]
    except sqlite3.OperationalError as exc:
        if 'no such' not in str(exc):
            raise
        return

    latest = {event['task_id']: (event['kind'], event['payload'])
              for event in sorted(events, key=_event_order)}
    for row in rows:
        row.pop('retained_stop', None)
        tid = str(row['id'])
        if tid not in tasks or any(value is not None and value != '' for value in tasks[tid]):
            continue
        kind, raw = latest.get(tid, (None, None))
        if kind != 'decision_retained':
            continue
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        disposition = payload.get('disposition')
        if not isinstance(disposition, dict) or disposition.get('reason') != 'scope_stopped':
            continue
        row['retained_stop'] = {k: payload[k] for k in ('episode_uid', 'event_uid') if payload.get(k)}
        row['retained_stop'].update({k: disposition[k] for k in ('reason', 'summary', 'evidence') if k in disposition})
