"""One advisory per held-prerequisite episode and recorded audience. [daveey]

Publication never changes a dependency, renews scope or creates an executor.
A vanished delivery subscription alone is not a new decision to publish.
"""
from __future__ import annotations

import hashlib
import json

from hermes_cli.kanban_db import _json_dict


def current(conn, child: str) -> list[dict]:
    """Read actual stop events, not maintenance notices that happen to be newer."""
    from hermes_cli.kanban_sync_divergence import _event_order
    out = []
    for row in conn.execute(
            "SELECT p.id,p.status FROM tasks p JOIN task_links l ON l.parent_id=p.id "
            "JOIN tasks c ON c.id=l.child_id WHERE c.id=? AND c.status NOT IN ('done','archived') "
            "AND p.status IN ('triage','failed') ORDER BY p.id", (child,)):
        stops = [dict(e) for e in conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND kind IN "
            "('created','spend_budget_triaged','decision_hold_restricted','decision_retained','gave_up','failure_finalized') "
            "OR (task_id=? AND kind='status' AND json_valid(payload) AND json_extract(payload,'$.status') IN ('triage','failed'))",
            (row[0], row[0]))]
        if stops:
            event = max(stops, key=_event_order)
            out.append({'parent_id': row[0], 'parent_status': row[1],
                        'episode': event.get('uid') or str(event['id']),
                        'reason': _json_dict(event['payload']).get('reason') or 'Parent requires a decision'})
    return out


def _pending(conn, child):
    from hermes_cli.kanban_hold_audience import describe
    authority = describe(conn, child)
    # Running creator sessions come and go without changing recorded ownership.
    audience = {'chats': sorted(authority['chats'], key=lambda c: json.dumps(c, sort_keys=True)),
                'creator_task_id': authority['creator_task_id']}
    for signal in current(conn, child):
        revision = hashlib.sha256(json.dumps([signal, audience], sort_keys=True).encode()).hexdigest()
        if not conn.execute(
                "SELECT 1 FROM task_events WHERE task_id=? AND kind='parent_held' "
                "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.revision') END=?",
                (child, revision)).fetchone():
            yield {**signal, 'authority': authority, 'revision': revision}


def sweep(conn) -> None:
    """Canonical-home publication before capacity admission; unchanged ticks are read-only."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_dispatch_storage import DispatchWriteContended
    from hermes_cli.kanban_hold_notices import bind, creator
    from hermes_cli.kanban_wake_check import foreign_ids
    copies = foreign_ids(conn)
    children = [r[0] for r in conn.execute(
        "SELECT DISTINCT c.id FROM tasks c JOIN task_links l ON l.child_id=c.id "
        "JOIN tasks p ON p.id=l.parent_id WHERE c.status NOT IN ('done','archived') "
        "AND p.status IN ('triage','failed')")]
    for child in children:
        if child in copies:
            continue
        try:
            if not list(_pending(conn, child)):
                continue
            with kb.write_txn(conn):
                for signal in _pending(conn, child):
                    bind(conn, child)
                    kb._append_event(conn, child, 'parent_held', signal)
                    creator(conn, child, 'parent:' + signal['revision'], text(signal))
        except DispatchWriteContended:
            raise
        except Exception:  # health: allow BLE001 -- rollback this advisory and retry; never satisfy its edge
            kb._log.warning('kanban: parent-held publication failed for %s', child, exc_info=True)


def text(payload: dict) -> str:
    return (f"Parent {payload.get('parent_id')} is held in {payload.get('parent_status')}: "
            f"{payload.get('reason')}. The creator/owning chat must disposition this prerequisite. "
            "The dependency remains open; this notice grants no scope renewal, execution or acceptance.")
