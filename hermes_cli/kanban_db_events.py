"""Event append seam: stamp portable recovery and wake-episode evidence atomically."""
from __future__ import annotations

import json
import sqlite3
import time


def latest_event(conn: sqlite3.Connection, task_id: str, kind: str, run_id: int | None = None):
    """Newest event payload of a kind, optionally scoped to one run."""
    sql = "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?"
    params = (task_id, kind)
    if run_id is not None:
        sql += " AND run_id = ?"
        params = (*params, int(run_id))
    return conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()


def append_event(conn: sqlite3.Connection, task_id: str, kind: str, payload: dict | None = None,
                 *, run_id: int | None = None) -> None:
    from hermes_cli.kanban_recovery_history import stamp
    from hermes_cli import kanban_wake_episode as episode

    from hermes_cli.kanban_run_models import stamp as stamp_models

    payload = stamp_models(conn, task_id, kind, payload, run_id)
    # Historical testimony is not a new lifecycle/wake observation.
    evidence = None
    if kind != 'run_models_attested':
        payload, evidence = episode.snapshot_event(conn, task_id, kind, payload)
    if evidence is not None:  # precedes the primary event, which stays the latest
        cur = conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)", (
            task_id, run_id, episode.EVIDENCE_EVENT, json.dumps(evidence), int(time.time())))
        assert cur.lastrowid is not None
        episode.consumed(conn, task_id, cur.lastrowid)
    stamped = stamp(conn, task_id, kind, payload, run_id)
    cur = conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)", (
        task_id, run_id, kind, json.dumps(stamped, ensure_ascii=False) if stamped else None, int(time.time())))
    if payload is not None and "_wake_episode_v1" in payload:
        assert cur.lastrowid is not None
        episode.consumed(conn, task_id, cur.lastrowid)
