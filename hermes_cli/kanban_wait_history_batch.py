"""Invocation-owned ETA history: one bounded capture per kind, shared by all targets.

Display only. Literal ``samples`` and its callers keep their existing semantics.
No connection or authority survives this capture. [daveey]
"""
from __future__ import annotations

import json
import sqlite3

from hermes_cli import kanban_wait_info as kwi


def capture(conn: sqlite3.Connection, kind: str) -> dict[str, list[float]]:
    from hermes_cli.kanban_wait_history import END_EVENTS, SCAN_CAP

    # Probe each end kind through (kind, task_id, id), then choose the first id,
    # NOT the smallest timestamp. The old ORDER BY id subquery could scan all
    # later unrelated events for each unfinished park.
    probes = ' UNION ALL '.join(
        'SELECT MIN(n.id) AS next_id FROM task_events n '
        'WHERE n.kind=? AND n.task_id=p.task_id AND n.id>p.id' for _ in END_EVENTS)
    # Explicit opt-in keeps this partial index unavailable to existing readers
    # whose unspecified traversal order must not change after installation.
    indexed = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='index' AND name='idx_events_park_history'"
    ).fetchone() is not None
    source = 'task_events INDEXED BY idx_events_park_history' if indexed else 'task_events'
    opt_in = " AND typeof(kind)='text'" if indexed else ''
    sql = (
        f"WITH parks AS MATERIALIZED (SELECT id,task_id,payload,created_at FROM {source} "
        "WHERE kind IN ('scheduled','continued')" + opt_in + " AND json_extract(payload,'$.awaiting.kind')=? "
        "ORDER BY id DESC LIMIT ?), ended AS ("
        f"SELECT p.*, (SELECT MIN(next_id) FROM ({probes})) AS end_id FROM parks p) "
        "SELECT json_group_array(json_array(CASE WHEN typeof(payload)='blob' THEN hex(payload) ELSE payload END,created_at,"
        "(SELECT created_at FROM task_events WHERE id=end_id),typeof(payload)='blob')) "
        "FROM (SELECT * FROM ended ORDER BY id DESC)")
    packed = conn.execute(sql, (kind, SCAN_CAP, *END_EVENTS)).fetchone()[0]
    out: dict[str, list[float]] = {}
    for payload, parked_at, ended_at, blob in json.loads(packed):
        if ended_at is None or parked_at is None or float(ended_at) <= float(parked_at):
            continue
        try:
            if blob:
                payload = bytes.fromhex(payload)
            awaiting = json.loads(payload).get('awaiting') or {}
        except (ValueError, AttributeError, TypeError):
            continue
        target = kwi.wait_target(kind, awaiting.get('ref'))
        bucket = out.setdefault(target, [])
        if len(bucket) < kwi.ETA_HISTORY:
            bucket.append(float(ended_at) - float(parked_at))
    return out
