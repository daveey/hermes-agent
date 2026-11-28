"""Home-owned, atomic replan prerequisites; executor admission is read-only everywhere."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

AUTHOR = 'kanban-auto-replan'
REQUESTED = 'auto_replan_requested'
OWNER = 'auto_replan_owner'
DEFAULT_RUNS = 8


@dataclass(frozen=True)
class State:
    episode: str
    count: int
    threshold: int
    baseline: tuple[str, ...]
    parent: str | None = None

    @property
    def due(self):
        return self.parent is not None or self.count >= self.threshold


def _run_keys(conn, tid):
    rows = conn.execute("SELECT * FROM task_runs WHERE task_id=? AND status != 'pending_sync'", (tid,)).fetchall()
    return {str(r['uid'] if 'uid' in r.keys() and r['uid'] else f'local:{r["id"]}'): r for r in rows}


def _requests(conn, tid):
    from hermes_cli.kanban_sync_divergence import _event_order
    rows = conn.execute('SELECT * FROM task_events WHERE task_id=? AND kind=?', (tid, REQUESTED)).fetchall()
    return [json.loads(row['payload']) for row in sorted(rows, key=lambda r: _event_order(dict(r)))]


def _parent_status(conn, pid):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_foreign_card as foreign
    task = kb.get_task(conn, pid)
    if task is not None:
        return task.status
    with foreign.open_replica(foreign._live_db_file(conn)) as replica:
        task = kb.get_task(replica, pid) if replica is not None else None
        if task is not None:
            return task.status
    from hermes_cli.kanban_db_sync import sync_enabled
    if sync_enabled():
        has_tombstones = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_sync_tombstones'").fetchone()
        deleted = has_tombstones and conn.execute(
            "SELECT 1 FROM kanban_sync_tombstones WHERE table_name='tasks' AND row_uid=?", (pid,)).fetchone()
        if not deleted:
            return 'unknown'  # Missing from partial sync is not proof of cancellation.
    return None


def _threshold(conn, tid, rows, baseline):
    from hermes_cli.kanban_release import current_mandate
    row = conn.execute('SELECT max_retries FROM tasks WHERE id=?', (tid,)).fetchone()
    threshold = max(DEFAULT_RUNS, (row[0] or 0) if row is not None else 0)
    mandate = current_mandate(conn, tid)
    if mandate and type(mandate.get('runs')) is int and mandate['runs'] > 0:
        released = conn.execute('SELECT created_at FROM task_events WHERE id=?', (mandate['_id'],)).fetchone()
        if released:
            # A release grants N MORE runs. Do not interrupt it merely because older attempts exist.
            prior = sum(r['started_at'] <= released[0] for key, r in rows.items() if key not in baseline)
            threshold = max(threshold, prior + mandate['runs'])
    return threshold


def state(conn, tid) -> State | None:
    from hermes_cli.kanban_replan_apply import published, read_plan
    if conn.execute('SELECT 1 FROM task_events WHERE task_id=? AND kind=?', (tid, OWNER)).fetchone():
        return None  # Replan cards use the existing three-failure path, never recursive replans.
    rows = _run_keys(conn, tid)
    episode, baseline = 'initial', ()
    for request in _requests(conn, tid):
        parent = request['parent']
        status = _parent_status(conn, parent)
        if status in (None, 'archived', 'done', 'unknown') and published(conn, tid, parent):
            # The original owns this durable reset even after its plan parent is retired.
            episode, baseline = parent, tuple(request['baseline'])
            continue
        if status in (None, 'archived') or (status == 'done' and not (read_plan(conn, parent) or '').strip()):
            # Cancellation is not a completed plan or a counter reset. Replace its prerequisite.
            episode = f'replace-{parent}'
            continue
        return State(request['episode'], len(set(rows) - set(baseline)), request['threshold'],
                     tuple(request['baseline']), parent)
    threshold = _threshold(conn, tid, rows, baseline)
    return State(episode, len(set(rows) - set(baseline)), threshold, tuple(rows))


def claim_refused(conn, tid):
    current = state(conn, tid)
    if current is None or not current.due:
        return False
    from hermes_cli import kanban_db_sync as sync
    sync.defer_claim(tid, 'auto_replan_prerequisite', sync.ORIGIN_LOCAL)
    return True


def _body(tid):
    return (
        f'Replan original card {tid}; this is its prerequisite, not a replacement goal. '
        'Read the original full body/acceptance, checkpoints, recent run outcomes and comments '
        'with kanban_show(verbose=true). Diagnose why it did not converge. Write a concrete new plan '
        'back onto the original (comment plus updated body/acceptance section), or split the original '
        'into smaller prerequisite cards linked as its parents. If the goal is already met or obsolete, '
        'complete/archive it with a stated, evidenced reason. Do not weaken acceptance. '
        'Acceptance, safety, permission or budget changes require one human_request on the original; '
        'do not invent approval or relax existing review, CI, budget or stop guards. '
        'Preserve any existing owner release/goal mandate. Complete this replan only after the new plan '
        'is durably recorded and read back. If the original is foreign and cannot be edited directly, '
        'record the actionable plan in this replan completion summary/result: its home appends that '
        'handoff to the original body and comment, without replacing acceptance, before it resumes. '
        'Use the supported operator comment route for foreign-card communication; do not bypass custody. '
        'The original resumes automatically through its dependency '
        'when this card completes. This card is exempt from auto-replan and escalates through the '
        'existing stuck-card path after three failures.')


def hold(conn, tid):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_wake_check import foreign_copy
    from hermes_cli.kanban_home_transfer import frozen
    from hermes_cli.kanban_containment_connections import held
    from hermes_cli.kanban_spend_decision import pending
    if foreign_copy(conn, tid) or frozen(conn, tid):
        return None
    current = state(conn, tid)
    if current is None or not current.due:
        return None
    with kb.write_txn(conn):
        task = kb.get_task(conn, tid)
        if (foreign_copy(conn, tid) or frozen(conn, tid) or held(conn, tid) or pending(conn, tid)
                or task is None or task.status not in ('ready', 'review') or task.current_run_id is not None
                or (task.claim_lock and (task.claim_expires or 0) > int(time.time()))):
            return None
        current = state(conn, tid)
        if current is None or not current.due:
            return None
        if current.parent and _parent_status(conn, current.parent) == 'unknown':
            return current.parent  # Keep the existing prerequisite; await normal sync, never duplicate it.
        if current.parent and _parent_status(conn, current.parent) == 'done':
            from hermes_cli.kanban_replan_apply import apply
            if apply(conn, tid, current.parent):
                return None
        if current.parent:
            parent = current.parent
        else:
            key = f'replan:{tid}:{current.episode}'
            existing = conn.execute('SELECT id FROM tasks WHERE idempotency_key=?', (key,)).fetchone()
            parent = existing[0] if existing else kb.create_task(
                conn, title=f'Replan nonconverging card {tid}', body=_body(tid),
                assignee=task.assignee or 'default', tenant=task.tenant, priority=task.priority,
                created_by=AUTHOR, creator_task_id=tid, idempotency_key=key,
                workspace_kind='scratch', max_retries=3)
            kb._append_event(conn, parent, OWNER, {'original': tid, 'episode': current.episode})
            kb._append_event(conn, tid, REQUESTED, {
                'episode': current.episode, 'parent': parent, 'threshold': current.threshold,
                'count': current.count, 'baseline': current.baseline, 'source_status': task.status})
            kb.add_comment(conn, tid, AUTHOR,
                           f'{current.count} runs since the last completed replan. Replan parent {parent} '
                           'owns a new convergence plan; this card waits in todo and resumes on completion.', system=True)
        kb.link_tasks(conn, parent, tid, allow_nested=True)
        # link_tasks demotes ready only; preserve the review lane through the existing dependency event.
        if task.status == 'review':
            conn.execute("UPDATE tasks SET status='todo' WHERE id=? AND status='review'", (tid,))
            kb._append_event(conn, tid, 'dependency_wait', {'source_status': 'review', 'reason': 'auto_replan'})
    return parent
