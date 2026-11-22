"""Request-local evidence shared by lanes flow consumers. [daveey]

Never retain this object across requests: row/event changes must be visible on the
next projection. PR provider observations have a separate bounded lifecycle.
"""
from __future__ import annotations


class Evidence:
    """Read each card's ordered history and parent state at most once per view."""

    def __init__(self, conn, *, rows=None):
        from hermes_cli import kanban_lanes_flow as flow

        self.conn = conn
        self.rows = (conn.execute(
            f"SELECT * FROM tasks WHERE status IN {flow._OPEN} ORDER BY created_at, id").fetchall()
            if rows is None else rows)
        self._identity = tuple(tuple(r) for r in conn.execute('PRAGMA database_list'))
        if not any(r[2] for r in self._identity):
            self._identity = ('memory', id(conn))
        self._rows = {row["id"]: row for row in self.rows}
        self._events = {}
        self._parents = {}
        self._urls = {}

    def connection(self, task_id):
        return self.conn

    def identity(self, task_id):
        return self._identity

    def row(self, task_id):
        if task_id not in self._rows:
            self._rows[task_id] = self.conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._rows[task_id]

    def events(self, task_id):
        from hermes_cli import kanban_lanes_flow as flow

        if task_id not in self._events:
            # Every projection visits the open cards. Read their histories in
            # bounded batches on first demand; terminal parents remain lazy.
            ids = list(dict.fromkeys([row['id'] for row in self.rows
                                      if row['id'] not in self._events] + [task_id]))
            loaded = {tid: [] for tid in ids}
            for start in range(0, len(ids), 400):
                batch = ids[start:start + 400]
                marks = ','.join('?' for _ in batch)
                events = self.conn.execute(
                    f"SELECT task_id, id, kind, payload FROM task_events "
                    f"WHERE task_id IN ({marks}) ORDER BY task_id, id", batch).fetchall()
                for event in events:
                    loaded[event['task_id']].append(
                        {"id": event["id"], "kind": event["kind"], "payload": flow._payload(dict(event))})
            # A failed batch must not leave a partially populated view.
            self._events.update(loaded)
        return self._events[task_id]

    def parents(self, task_id):
        from hermes_cli import kanban_db as kb

        if task_id not in self._parents:
            self._parents[task_id] = kb.unsatisfied_parents(self.conn, task_id)
        return self._parents[task_id]

    def pr_url(self, task):
        from hermes_cli import kanban_lanes_flow as flow

        task_id = task["id"]
        if task_id not in self._urls:
            events = self.events(task_id)
            url = flow._pr_url(task, events)
            awaited = flow._awaiting(task, events)
            if not url and isinstance(awaited, dict):
                ref = awaited.get("ref")
                if isinstance(ref, str) and ref.startswith("https://github.com/") and "/pull/" in ref:
                    url = ref
            self._urls[task_id] = url
        return self._urls[task_id]

    def blockers(self, task_id):
        out = []
        for parent_id, _status in self.parents(task_id):
            parent = self.row(parent_id)
            out.append({"id": parent_id, "pr": self.pr_url(parent) if parent is not None else None})
        return out
