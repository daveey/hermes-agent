"""Move-scoped mirror of dependency results; never an exemption from PR policy.

The primary file owns only this gateway's cards. Moving a parked owner must not
lose the parent results the ordinary dependency/review checks need. Refresh these
from the Board replica together with foreign parent statuses, not from move prose.
"""
from __future__ import annotations

import json

_TABLE = "kanban_move_parent_results"


def prepare(live, replica, wanted):
    from hermes_cli import kanban_db as kb

    # Do not copy arbitrary foreign histories: only dependencies of cards whose
    # operator explicitly supplied a queued-move checkpoint need this evidence.
    scoped = set()
    for event in live.execute(
        "SELECT e.task_id, e.payload FROM tasks t JOIN task_events e ON e.id = "
        "(SELECT MAX(m.id) FROM task_events m WHERE m.task_id = t.id AND m.kind = 'moved') "
        "WHERE t.status NOT IN ('done', 'archived')"
    ):
        payload = kb._json_dict(event["payload"])
        checkpoint = payload.get("checkpoint")
        if payload.get("queued") is True and isinstance(checkpoint, str) and checkpoint.strip():
            scoped.update(kb.parent_ids(live, event["task_id"]))
    values = []
    for parent in sorted(set(wanted) & scoped):
        row = replica.execute(
            "SELECT r.*, t.status AS parent_status, EXISTS (SELECT 1 FROM task_events e "
            "WHERE e.task_id = r.task_id AND e.run_id = r.id AND e.kind = 'claimed') AS was_claimed "
            "FROM task_runs r JOIN tasks t ON t.id = r.task_id WHERE t.id = ? "
            "ORDER BY r.id DESC LIMIT 1", (parent,),
        ).fetchone()
        if row is not None:
            values.append((parent, json.dumps(dict(row))))
    exists = live.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_TABLE,)).fetchone()
    current = dict(live.execute(f"SELECT parent_id, payload FROM {_TABLE}")) if exists else {}
    updated = dict(values)
    changed = sorted(k for k in set(current) | set(updated) if current.get(k) != updated.get(k))
    return values, changed


def apply(live, values):
    """Caller owns the transaction shared with the parent-status refresh."""
    live.execute(f"CREATE TABLE IF NOT EXISTS {_TABLE} (parent_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    live.execute(f"DELETE FROM {_TABLE}")
    live.executemany(f"INSERT INTO {_TABLE} VALUES (?, ?)", values)


def result(conn, task_id, parent_id):
    """Read only for a checkpointed queued move of an orderly parked owner."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_unused_grant import latest_started_run

    if kb.get_task(conn, parent_id) is not None:
        return None  # A local parent's own rows always decide; no stale mirror fallback.
    if parent_id not in dict(kb.parent_states(conn, task_id)):
        return None
    move = conn.execute(
        "SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind = 'moved' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if move is None:
        return None
    payload = kb._json_dict(move["payload"])
    checkpoint = payload.get("checkpoint")
    if payload.get("queued") is not True or not isinstance(checkpoint, str) or not checkpoint.strip():
        return None
    run = latest_started_run(conn, task_id)
    if run is None or run["outcome"] != "blocked":
        return None
    ended = kb._to_epoch(run["ended_at"])
    moved = kb._to_epoch(move["created_at"])
    if ended is None or moved is None or moved < ended:
        return None
    faults = conn.execute(
        "SELECT created_at FROM task_events WHERE task_id = ? "
        "AND kind IN ('crashed', 'reclaimed', 'timed_out')", (task_id,),
    )
    if any((kb._to_epoch(e[0]) or 0) >= ended for e in faults):
        return None
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_TABLE,)).fetchone():
        return None
    row = conn.execute(f"SELECT payload FROM {_TABLE} WHERE parent_id = ?", (parent_id,)).fetchone()
    return kb._json_dict(row[0]) if row else None
