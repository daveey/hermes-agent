"""Durable quota effects after an authoritative neutral release. [daveey]

The episode, notice and comment commit together with a fixed wall deadline.
A failed wall write leaves the neutral receipt pending; retry never moves the
reset deadline. Admission waits for this receipt, including split feed pages.
"""
import json
import time


def prepare(conn, row):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_quota_episode as quota
    from hermes_cli import kanban_provider_walls as walls

    conn.execute("CREATE TABLE IF NOT EXISTS kanban_quota_effects ("
                 "run_uid TEXT PRIMARY KEY REFERENCES kanban_failure_commands(run_uid) ON DELETE CASCADE, "
                 "state TEXT NOT NULL)")
    stored = conn.execute("SELECT state FROM kanban_quota_effects WHERE run_uid=?", (row['run_uid'],)).fetchone()
    if stored:
        state = json.loads(stored[0])
    else:
        quota.record_episode(conn, row['task_id'], row['run_id'], captured=True)
        event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                             "AND kind='quota_episode'", (row['task_id'], row['run_id'])).fetchone()
        churn = quota.episode(conn, row['task_id'], run_id=row['run_id']) if event is None else None
        if churn and churn['run_id'] == row['run_id'] and churn.get('auth_churn'):
            state = {**churn, 'write_wall': False}  # [daveey] auth churn: no episode event, no wall
        elif event is None:
            raise RuntimeError('quota release is not the latest ended run; refusing admission')
        else:
            state = kb._json_dict(event[0])
            state['write_wall'] = bool(state.get('provider') and state['until'] - time.time() >= walls.MIN_WALL_SECONDS)
            state['wall_model'] = walls.receipt_wall_model(conn, row['task_id'], row['run_id'], state.get('provider'))
        conn.execute("INSERT INTO kanban_quota_effects (run_uid,state) VALUES (?,?)",
                     (row['run_uid'], json.dumps(state)))
    return state


def finish(state):
    if state['write_wall'] and state['until'] > time.time():
        from hermes_cli.kanban_provider_walls import wall_key
        persist_wall(wall_key(state['provider'], state.get('wall_model')), state['until'])


def persist_wall(provider, until, *, now=None, path=None):
    """Write a fixed deadline, propagating failure so the receipt stays pending."""
    from hermes_cli import kanban_provider_walls as walls
    now = time.time() if now is None else now
    path = path or walls.walls_path()
    current = walls.active_walls(now=now, path=path)
    current[provider] = max(until, current.get(provider, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(current), encoding='utf-8')
    temporary.replace(path)


def pending_task(conn, task_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_neutral_effects'").fetchone():
        return False
    from hermes_cli.kanban_failure_release import release_predicate
    return conn.execute("SELECT 1 FROM kanban_neutral_effects e JOIN kanban_failure_commands c USING(run_uid) "
                        f"WHERE c.task_id=? AND e.done=0 AND e.event_kind='rate_limited' AND {release_predicate(conn)} LIMIT 1",
                        (task_id,)).fetchone() is not None
