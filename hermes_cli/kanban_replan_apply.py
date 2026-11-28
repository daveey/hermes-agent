"""Publish a portable replan handoff at the original card's home, without replacing acceptance."""
from __future__ import annotations

import json

APPLIED = 'auto_replan_applied'


def published(conn, tid, parent):
    for row in conn.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=?', (tid, APPLIED)):
        if json.loads(row[0]).get('parent') == parent:
            return True
    return False


def _plan(conn, parent):
    from hermes_cli import kanban_db as kb
    task = kb.get_task(conn, parent)
    if task is None or task.status != 'done':
        return None
    return kb.latest_summary(conn, parent) or task.result


def read_plan(conn, parent):
    from hermes_cli import kanban_foreign_card as foreign
    plan = _plan(conn, parent)
    if not plan:
        with foreign.open_replica(foreign._live_db_file(conn)) as replica:
            plan = _plan(replica, parent) if replica is not None else None
    return plan


def apply(conn, tid, parent):
    """Caller owns the home writer and has already rechecked holds/claim/status."""
    from hermes_cli import kanban_db as kb
    if published(conn, tid, parent):
        return True
    plan = read_plan(conn, parent)
    if not plan or not plan.strip():
        return False  # A done marker alone cannot substitute for a recorded plan.
    task = kb.get_task(conn, tid)
    if task is None:
        return False
    text = f'## Replan handoff from {parent}\n{plan.strip()}'
    kb.add_comment(conn, tid, 'kanban-auto-replan', text, system=True)
    # Append-only: preserve every original acceptance, safety, permission and budget line.
    conn.execute('UPDATE tasks SET body=? WHERE id=?', ((task.body or '') + '\n\n' + text, tid))
    kb._append_event(conn, tid, APPLIED, {'parent': parent, 'mode': 'append_only_handoff'})
    return True
