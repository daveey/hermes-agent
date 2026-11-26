"""Order existing unacknowledged task envelopes before their dependent rows. [daveey]"""
from __future__ import annotations

import json


def reassessment_supported(cap) -> bool:
    """True only for a valid serving capability advertising owner_reassessment_v1 == 1."""
    return bool(cap) and type(cap.get('owner_reassessment_v1')) is int and cap['owner_reassessment_v1'] == 1


def select_pending(conn, hub, board, seen, *, now: int, limit: int, before_head: bool, origin_cap: dict | None = None):
    """Dependency selection and the existing pre-head fence share one batch.

    ``origin_cap`` is the caller's one per-push capability snapshot; omitted, it is read normally.
    """
    if origin_cap is None:
        from hermes_cli.kanban_sync_status_origin import capability
        origin_cap = capability(hub, board)
    certified = reassessment_supported(origin_cap)
    from hermes_cli import kanban_sync_backlog as backlog
    from hermes_cli.kanban_sync_quarantine import pending_rows

    from hermes_cli.kanban_actionability_transport import eligible_pending
    rows = selected = parent_first(conn, eligible_pending(conn, pending_rows(conn, now), hub, board), now, limit,
                                   reassessment=certified)
    stopped = False
    if before_head:
        rows, stopped = backlog.sendable_before_head(conn, hub, board, rows, seen)
        rows = parent_first(conn, rows, now, limit, allowed={r['op_id'] for r in rows}, reassessment=certified)
    return rows, selected, stopped


def parent_first(conn, rows, now: int, limit: int, *, allowed: set[str] | None = None,
                 reassessment: bool = False) -> list[dict]:
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
        from hermes_cli.kanban_sync_evidence_order import firsts
        for event in [] if blocked else firsts(conn, row, reassessment=reassessment):
            # The status op waits behind its own event exactly as a child waits behind its parent.
            if ((event['retry_after'] or 0) > now or not sendable(conn, [event])
                    or (allowed is not None and event['op_id'] not in allowed)):
                blocked = True
                break
            if event['op_id'] not in included:
                required.append(event)
        if blocked:
            continue
        from hermes_cli.kanban_owner_reassessment_transport import pending_park_parent
        certificate = pending_park_parent(conn, row)
        frozen = json.loads(certificate['wire_json']) if certificate is not None and certificate.get('wire_json') else {}
        if certificate is not None and (reassessment or 'owner_reassessment_v1' in frozen.get('fields', {})):
            # A frozen certificate cannot fall back when support disappears.
            # Never let its separately captured park consume the uid first.
            if (not reassessment or (certificate['retry_after'] or 0) > now
                    or not sendable(conn, [certificate])
                    or (allowed is not None and certificate['op_id'] not in allowed)):
                continue
            if certificate['op_id'] not in included:
                dependency = parent_first(conn, [certificate], now, limit, allowed=allowed,
                                          reassessment=reassessment)
                if not any(p['op_id'] == certificate['op_id'] for p in dependency):
                    continue
                required.extend(dependency)
        # Keep both link parents together with their child when possible. If the
        # batch is full, the ordinary next round drains the remaining FIFO rows.
        for item in required + [row]:
            if item['op_id'] not in included and len(out) < limit:
                out.append(item)
                included.add(item['op_id'])
    return out
