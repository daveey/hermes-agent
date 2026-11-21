"""Order existing unacknowledged task envelopes before their dependent rows. [daveey]"""
from __future__ import annotations

import json


def select_pending(conn, hub, board, seen, *, now: int, limit: int, before_head: bool):
    """Dependency selection and the existing pre-head fence share one batch."""
    from hermes_cli import kanban_sync_backlog as backlog
    from hermes_cli.kanban_sync_quarantine import pending_rows

    rows = selected = parent_first(conn, pending_rows(conn, now), now, limit)
    stopped = False
    if before_head:
        rows, stopped = backlog.sendable_before_head(conn, hub, board, rows, seen)
        rows = parent_first(conn, rows, now, limit, allowed={r['op_id'] for r in rows})
    return rows, selected, stopped


def parent_first(conn, rows, now: int, limit: int, *, allowed: set[str] | None = None) -> list[dict]:
    """Promote the existing FIFO parent, never synthesize a competing retry identity.

    Only an unacknowledged card needs this dependency ordering. A parent's backoff
    or quarantine also holds its children, without charging them refusal attempts.
    Wire envelopes and the durable FIFO timestamps are not rewritten.
    """
    from hermes_cli.kanban_sync_quarantine import sendable

    parents: dict[str, dict | None] = {}
    out: list[dict] = []
    included: set[str] = set()
    for row in rows:
        if len(out) >= limit:
            break
        needed = []
        if row['table_name'] == 'task_links' and row['op'] == 'upsert':
            needed = str(row['row_uid']).split('>', 1)
        elif row['table_name'] != 'tasks':
            fields = row['fields'] if isinstance(row['fields'], dict) else json.loads(row['fields'] or '{}')
            needed = [fields.get('task_id')]
        required = []
        blocked = False
        for tid in filter(None, needed):
            if tid not in parents:
                hit = conn.execute(
                    "SELECT o.* FROM kanban_sync_outbox o JOIN tasks t ON t.id=o.row_uid "
                    "WHERE o.table_name='tasks' AND o.state='pending' AND o.op!='release' "
                    "AND t.id=? AND t.sync_version IS NULL ORDER BY o.created_at,o.rowid LIMIT 1", (tid,)).fetchone()
                parents[tid] = dict(hit) if hit else None
            parent = parents[tid]
            if parent is None:
                continue  # normal synthetic-parent path, including its omission guard
            if ((parent['retry_after'] or 0) > now or not sendable(conn, [parent])
                    or (allowed is not None and parent['op_id'] not in allowed)):
                blocked = True
                break
            if parent['op_id'] not in included and all(p['op_id'] != parent['op_id'] for p in required):
                required.append(parent)
        if blocked:
            continue
        # Keep both link parents together with their child when possible. If the
        # batch is full, the ordinary next round drains the remaining FIFO rows.
        for item in required + [row]:
            if item['op_id'] not in included and len(out) < limit:
                out.append(item)
                included.add(item['op_id'])
    return out
