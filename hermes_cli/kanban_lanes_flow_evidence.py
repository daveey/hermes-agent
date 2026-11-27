"""Request-local evidence shared by lanes flow consumers. [daveey]

Never retain this object across requests: row/event changes must be visible on the
next projection. PR provider observations have a separate bounded lifecycle.
"""
from __future__ import annotations


import re


# Preserve every event that can affect flow classification, including episode
# boundaries that consume no payload. PR/head evidence is intentionally NOT
# restricted by kind: legacy/custom writers can attach it to any event.
_FLOW_KINDS = (
    'created', 'scheduled', 'continued', 'lane_gated', 'dispatch_skipped',
    'claimed', 'spawned', 'worker_bound', 'completed', 'reopened',
    'changes_requested', 'review_reopened', 'review_requested',
    'unblocked', 'archived',
)
_PR_KEYS = ('published_pr', 'reviewed_pr', 'pr', 'flow_pr', 'head', 'head_sha', 'headRefOid')


# A single conservative scan avoids repeated SQL instr scans over large payloads.
# Only Unicode escapes can encode letters in these JSON keys; retain all such
# input rather than trying to recognize escaped key spellings ourselves.
_PR_PAYLOAD = re.compile('"(?:' + '|'.join(_PR_KEYS) + ')"')


class Evidence:
    """Read each card's flow-relevant history and parent state once per view.

    ``events`` is projection evidence, not an unabridged audit history. Retain
    kind-based episode boundaries and arbitrary-kind PR/head observations.
    """

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
                kinds = ','.join('?' for _ in _FLOW_KINDS)
                # All unescaped PR keys contain 'pr'; head keys contain 'head'.
                # This cheap SQL superset avoids materializing obvious noise.
                # Exact key detection below also discards value-only matches.
                events = self.conn.execute(
                    f"SELECT task_id, id, kind, payload FROM task_events "
                    f"WHERE task_id IN ({marks}) AND (kind IN ({kinds}) "
                    "OR instr(payload, 'pr') > 0 OR instr(payload, 'head') > 0 "
                    "OR instr(payload, ?) > 0) ORDER BY task_id, id",
                    (*batch, *_FLOW_KINDS, '\\u')).fetchall()
                # Only the first creation and newest occurrence of each flow
                # kind can affect classification. Never compact PR/head-bearing
                # observations: their per-field updates are applied in order.
                latest = {}
                for event in events:
                    raw = event['payload']
                    has_pr = isinstance(raw, str) and ('\\u' in raw or _PR_PAYLOAD.search(raw))
                    if event['kind'] in _FLOW_KINDS and not has_pr:
                        key = (event['task_id'], event['kind'])
                        if event['kind'] != 'created' or key not in latest:
                            latest[key] = event
                    elif has_pr:
                        loaded[event['task_id']].append(event)
                for event in latest.values():
                    loaded[event['task_id']].append(event)
            for tid, history in loaded.items():
                loaded[tid] = [
                    {'id': e['id'], 'kind': e['kind'], 'payload': flow._payload(dict(e))}
                    for e in sorted(history, key=lambda e: e['id'])]
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
