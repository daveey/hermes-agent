"""Read-only shadow cards, collision-filtered and capped before loading history. [daveey]"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Callable

from hermes_cli import kanban_db
from hermes_cli import kanban_shadow_view as shadow_view
from plugins.kanban.dashboard import board_read, board_rows
from plugins.kanban.dashboard.board_incremental import rank_key


def clear() -> None:
    board_rows.clear()


def for_board(payload: dict, connect: Callable, query: tuple, diagnostic_inputs: Callable,
              diagnostics_from_inputs: Callable) -> dict:
    """Merge against fresh live ownership, preserving the live payload on read failure."""
    try:
        with closing(connect()) as live:
            return merge(payload, live, *query, diagnostic_inputs=diagnostic_inputs,
                         diagnostics_from_inputs=diagnostics_from_inputs)
    except sqlite3.Error:
        return payload


def _selection(conn, filters, live_ids, live_done, cap):
    """Narrow exact inventory; only displayed IDs reach full-row/history readers.

    Parse completion times just as Task.from_row does (legacy SQLite values can be
    text), and use the same combined ranking as the final done column.
    """
    rows = kanban_db.list_task_rows(conn, columns="id, status, completed_at", order_by="created", **filters)
    remote = [(r['id'], kanban_db._lossy_text(r['id']), kanban_db._lossy_text(r['status']),
               kanban_db._to_epoch(kanban_db._lossy_text(r['completed_at'])))
              for r in rows if kanban_db._lossy_text(r['id']) not in live_ids]
    done = [(tid, epoch) for _raw, tid, status, epoch in remote if status == 'done']
    keep = {tid for tid, _epoch in sorted([*done, *live_done], key=rank_key)[:cap]} if cap > 0 else None
    # Preserve raw SQL key types for the keyed fetch; Task.from_row normalizes display fields.
    ids = [raw for raw, tid, status, _epoch in remote if status != 'done' or keep is None or tid in keep]
    return ids, len(done)


def merge(payload: dict, live, tenant, include_archived, workflow_template_id, current_step_key, done_limit, *,
          diagnostic_inputs: Callable, diagnostics_from_inputs: Callable) -> dict:
    """Keep the exact remote total separately from its bounded display materialization."""
    with shadow_view.open_shadow_replica(live) as replica:
        if replica is None:
            return payload
        live_file, replica_file = shadow_view.db_file(live), shadow_view.db_file(replica)
        # Include even archived / filtered / capped-out live IDs: ownership precedes selection.
        live_ids = frozenset(r[0] for r in live.execute('SELECT id FROM tasks')) | frozenset(
            t['id'] for col in payload['columns'] for t in col['tasks'])
        live_done = tuple((t['id'], t['completed_at']) for col in payload['columns']
                          if col['name'] == 'done' for t in col['tasks'])
        cap = int(done_limit or 0)
        filters = dict(tenant=tenant, include_archived=include_archived,
                       workflow_template_id=workflow_template_id, current_step_key=current_step_key)
        key = (replica_file, 'shadow', live_file, tenant, bool(include_archived),
               workflow_template_id, current_step_key, cap, live_ids, live_done)

        def build(conn, prev=None, changed=None):
            ids, total = _selection(conn, filters, live_ids, live_done, cap)
            # Without trusted content marks, count/max cannot detect same-ID history edits.
            # Reload only the selected cards; ordinary board_read reuse stays unchanged.
            if not isinstance(changed, (set, frozenset)):
                prev = None
            result = board_read.materialize(
                conn, tenant, include_archived, workflow_template_id, current_step_key, cap,
                diagnostic_inputs=diagnostic_inputs, prev=prev, changed=changed, selected_ids=ids)
            result['done_total'] = total
            result['homes'] = shadow_view.homes_of(conn, [t.id for t, _fields, _extra in result['cards']])
            return result

        def fallback():
            # No trusted observer/P2: still bounded, and all inputs share a read-only snapshot.
            replica.execute('BEGIN')
            try:
                return build(replica)
            finally:
                replica.rollback()

        try:
            rows = board_rows.incremental(key, replica_file, build, fallback)
        except Exception:  # health: allow BLE001 -- an unreadable replica must not take the live board down
            shadow_view._log.warning('kanban: shadow replica board read failed', exc_info=True)
            return payload
    # Never retain projections: clock/config/profile diagnostics are current on every poll.
    remote = board_read.project(rows, include_archived, diagnostics_from_inputs)
    by_name = {c['name']: c for c in remote['columns']}
    for col in payload['columns']:
        extra = by_name.get(col['name'])
        if extra is None:
            continue
        col['tasks'] = col['tasks'] + [shadow_view.mark(t, rows['homes'].get(t['id'])) for t in extra['tasks']]
        if col['name'] == 'done':
            col['tasks'].sort(key=lambda d: d['id'], reverse=True)
            col['tasks'].sort(key=lambda d: (d['completed_at'] is None, -(d['completed_at'] or 0)))
            total = col.get('total', 0) + rows['done_total']
            if cap > 0:
                col['tasks'] = col['tasks'][:cap]
            col['total'] = total
            col['omitted'] = total - len(col['tasks'])
    payload['tenants'] = sorted(set(payload['tenants']) | set(remote['tenants']))
    payload['assignees'] = sorted(set(payload['assignees']) | set(remote['assignees']))
    return payload
