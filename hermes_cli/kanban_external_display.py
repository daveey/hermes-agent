"""Selected external-watch inputs for one display invocation, never strict polling. [daveey]"""
from __future__ import annotations

import json
import sqlite3


class Capture:
    def __init__(self, conn: sqlite3.Connection, ids):
        from hermes_cli import kanban_external_watch as watch

        self.conn = conn
        self.own = watch.own_host(conn)
        self.rows = {}
        # One winning event per task/kind. No full histories or per-card cursors.
        packed = conn.execute(
            "SELECT json_group_array(json_array(task_id,kind,id,"
            "CASE WHEN typeof(payload)='blob' THEN hex(payload) ELSE payload END,created_at,typeof(payload)='blob')) "
            "FROM task_events WHERE id IN ("
            "SELECT MAX(id) FROM task_events WHERE task_id IN (SELECT value FROM json_each(?)) "
            "AND kind IN ('scheduled','continued','external_watch') GROUP BY task_id,kind)",
            (json.dumps(ids),)).fetchone()[0]
        for tid, kind, eid, payload, at, blob in json.loads(packed):
            if blob:
                payload = bytes.fromhex(payload)
            self.rows[tid, kind] = (eid, payload, at)
        self.wakes = {}
        try:
            self.wakes = {tid: (cmd, every) for tid, cmd, every in conn.execute(
                "SELECT task_id,cmd,every FROM task_wake_checks "
                "WHERE task_id IN (SELECT value FROM json_each(?))", (json.dumps(ids),))}
        except sqlite3.Error:
            pass  # optional machine-local table; same fallback as literal plan
        self.last_at = {}
        try:
            self.last_at = dict(conn.execute(
                "SELECT task_id,last_at FROM task_wake_checks WHERE task_id IN (SELECT value FROM json_each(?))",
                (json.dumps(ids),)))
        except sqlite3.Error:
            pass  # independent compatibility/error boundary in literal _shell_seen
        self._blockers = None

    def context(self, tid):
        from hermes_cli import kanban_external_watch as watch

        parks = [row for kind in ('scheduled', 'continued') if (row := self.rows.get((tid, kind))) is not None]
        if not parks:
            return None
        eid, raw, at = max(parks, key=lambda row: row[0])
        payload = watch._json(raw)
        awaiting = payload.get('awaiting') if isinstance(payload.get('awaiting'), dict) else None
        return {'event_id': int(eid), 'since': float(at) if at is not None else None,
                'payload': payload, 'awaiting': awaiting}

    def record(self, tid, after):
        from hermes_cli import kanban_external_watch as watch

        row = self.rows.get((tid, 'external_watch'))
        return watch._json(row[1]) if row is not None and row[0] > after else None

    def blockers(self):
        from hermes_cli import kanban_shared_blockers

        if self._blockers is None:
            self._blockers = kanban_shared_blockers.blocker_states(self.conn)
        return self._blockers
