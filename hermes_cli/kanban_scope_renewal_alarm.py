"""Flag step-sized mandate loops; never release or gate the source. [daveey]"""
from __future__ import annotations

from hermes_cli import kanban_db as kb

PREFIX = 'scope-renewal-loop:'
THRESHOLD = 5


def _incident(conn, tid):
    from hermes_cli.kanban_sync_divergence import _event_order
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM task_events WHERE task_id=? AND kind IN "
        "('scope_renewed','completed','review_requested','status')", (tid,))]
    renewals = []
    for event in sorted(rows, key=_event_order):
        payload = kb._json_dict(event['payload'])
        if event['kind'] in ('completed', 'review_requested') or (
                event['kind'] == 'status' and payload.get('status') in ('done', 'review')):
            renewals = []
        elif event['kind'] == 'scope_renewed':
            renewals.append(event)
    return renewals


def _file(conn, tid):
    renewals = _incident(conn, tid)
    if len(renewals) < THRESHOLD:
        return
    first = renewals[0]
    identity = f"{PREFIX}{tid}:{first.get('uid') or first['id']}"
    if conn.execute('SELECT 1 FROM tasks WHERE idempotency_key=?', (identity,)).fetchone():
        return
    from hermes_cli import kanban_db_sync as sync
    with kb.write_txn(conn):
        task = kb.get_task(conn, tid)
        if task is None or task.status in ('done', 'review', 'archived'):
            return
        renewals = _incident(conn, tid)
        if len(renewals) < THRESHOLD:
            return
        first = renewals[0]
        identity = f"{PREFIX}{tid}:{first.get('uid') or first['id']}"
        kb.create_task(
            conn, title=f'Widen step-sized scope mandates to the goal: {tid}',
            body=(f'{tid} has {len(renewals)} scope_renewed events without entering done or review. '
                  'Repeated step-sized mandates can stop at each next gate. Ask its owning chat to '
                  'reconcile the remaining acceptance and choose one bounded goal-sized mandate, '
                  'or explicitly stop the remaining scope. Read the source and current owner decisions '
                  'before acting. This incident is advisory, not a guard, permission, budget renewal '
                  'or dependency. Preserve all safety, spend, CI and independent-review gates.'),
            assignee=task.assignee, created_by='scope-renewal-alarm', creator_task_id=tid,
            tenant=task.tenant, workspace_kind='scratch',
            runs_on=['pool:worker'] if sync.board_mode(conn) != 'off' else None,
            idempotency_key=identity, require_scope_authority=True)


def sweep(conn):
    """Only each source's home files; foreign replicas must not duplicate incidents."""
    from hermes_cli.kanban_wake_check import foreign_ids
    foreign = foreign_ids(conn)
    rows = conn.execute("SELECT DISTINCT t.id FROM tasks t JOIN task_events e ON e.task_id=t.id "
                        "WHERE e.kind='scope_renewed' AND t.status NOT IN ('done','review','archived')").fetchall()
    for (tid,) in rows:
        if tid in foreign:
            continue
        try:
            _file(conn, tid)
        except Exception:  # health: allow BLE001 -- rollback this incident and preserve unrelated upkeep
            kb._log.warning('kanban: scope renewal alarm failed for %s', tid, exc_info=True)
