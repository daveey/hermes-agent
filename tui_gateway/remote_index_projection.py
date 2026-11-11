"""Share per-row notice routing projections across unrelated replica writes.

Only routing is cached: freshness, delivery and claims stay with the caller.
One file/gateway generation is retained and deleted/superseded rows are dropped.
"""
from __future__ import annotations

import json

_identity = None
_rows: dict = {}


def reset():
    global _identity, _rows
    _identity, _rows = None, {}


def _read_rows(conn, me, previous):
    """Warm reads fetch only changed payloads, with identities in the same snapshot."""
    from tui_gateway import kanban_remote_notices as remote

    if not previous:
        return remote._canonical(remote._task_rows(conn, me))
    conn.execute("BEGIN")
    try:
        rows = remote._canonical(conn.execute(
            "SELECT gw, key, seq, NULL FROM replica WHERE coll='tasks' AND gw != ?", (me,)
        ).fetchall())
        result = []
        for owner, key, seq, _ in rows:
            hit = previous.get((owner, key))
            if hit is not None and isinstance(seq, int) and seq > 0 and hit[0] == seq:
                result.append((owner, key, seq, None))
            else:
                data = conn.execute("SELECT data FROM replica WHERE coll='tasks' AND gw=? AND key=?",
                                    (owner, key)).fetchone()[0]
                result.append((owner, key, seq, data))
        return result
    finally:
        conn.rollback()


def snapshot(conn, identity, me):
    """Called under the notice reader lock; unknown sequences never grant reuse."""
    from tui_gateway import kanban_remote_notices as remote

    global _identity, _rows
    scope = (identity, me)
    previous = _rows if identity is not None and _identity == scope else {}
    cols = {row[1] for row in conn.execute("PRAGMA table_info(replica)")}
    if "seq" not in cols:
        previous = {}
    rows = _read_rows(conn, me, previous)
    updated, index = {}, {}
    for owner, key, seq, data in rows:
        if not me or owner == me:
            continue
        hit = previous.get((owner, key))
        reusable = "seq" in cols and isinstance(seq, int) and seq > 0
        if hit is None or not reusable or hit[0] != seq:
            try:
                task = json.loads(data)
            except (TypeError, ValueError):
                task = None
            chats = []
            if isinstance(task, dict) and task.get("status") in remote._TERMINAL and task.get("id"):
                chats = remote._linked_chats(task, me)
            hit = (seq, chats, {"gw": str(owner), "key": str(key), "task": task})
        updated[(owner, key)] = hit
        for chat in hit[1]:
            index.setdefault(chat, []).append(hit[2])
    _identity, _rows = scope, updated if identity is not None else {}
    return index
