"""Event append seam: stamp portable recovery and wake-episode evidence atomically."""
from __future__ import annotations

import json
import sqlite3
import time


def append_event(conn: sqlite3.Connection, task_id: str, kind: str, payload: dict | None = None,
                 *, run_id: int | None = None) -> None:
    from hermes_cli.kanban_recovery_history import stamp
    from hermes_cli import kanban_wake_episode as episode

    payload = episode.snapshot_event(conn, task_id, kind, payload)
    stamped = stamp(conn, task_id, kind, payload, run_id)
    cur = conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)", (
        task_id, run_id, kind, json.dumps(stamped, ensure_ascii=False) if stamped else None, int(time.time())))
    if payload is not None and "_wake_episode_v1" in payload:
        assert cur.lastrowid is not None
        episode.consumed(conn, task_id, cur.lastrowid)
