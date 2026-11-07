"""Retain remote-notice projections across unrelated replica commits.

The replica writer publishes a new seq for each changed row. Read identity rows
and changed payloads in one transaction; never cache time or claim decisions.
Only one replica/gateway generation is retained, with deleted rows discarded.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_identity = None
_rows: dict = {}


def reset() -> None:
    global _identity
    with _lock:
        _identity = None
        _rows.clear()


def _project(rows, me, remote):
    cards = list(remote._sweep_cards(rows, me))
    states = {ref: (state, {"sessions": task.get("sessions")})
              for ref, (state, task) in remote._current_states(rows).items()}
    return cards, states


def snapshot(conn, identity, me):
    """Caller owns a snapshot transaction; unknown identities/schemas do not memoize."""
    from tui_gateway import kanban_remote_notices as remote

    global _identity, _rows
    with _lock:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(replica)")}
        scope = (identity, me)
        if identity is None or "seq" not in cols:
            _identity, _rows = None, {}
            return _project(remote._canonical(remote._task_rows(conn, me)), me, remote)
        previous = _rows if _identity == scope else {}
        sql = "SELECT gw, key, seq, NULL FROM replica WHERE coll = 'tasks'"
        rows = remote._canonical(conn.execute(sql + " AND gw != ?", (me,)).fetchall()
                                 if me else conn.execute(sql).fetchall())
        # A cold pass reads payloads in one query, rather than thousands of seeks.
        cold = {(row[0], row[1]): row for row in remote._canonical(remote._task_rows(conn, me))} if not previous else {}
        updated = {}
        cards, states = [], {}
        for owner, key, seq, _ in rows:
            hit = previous.get((owner, key))
            if hit is None or hit[0] != seq:
                row = cold.get((owner, key))
                if row is None:
                    raw = conn.execute("SELECT data FROM replica WHERE coll='tasks' AND gw=? AND key=?",
                                       (owner, key)).fetchone()
                    row = (owner, key, seq, raw[0])
                projection = _project([row], me, remote)
                hit = (seq, projection)
            updated[(owner, key)] = hit
            cards.extend(hit[1][0])
            states.update(hit[1][1])
        _identity, _rows = scope, updated
        return cards, states
