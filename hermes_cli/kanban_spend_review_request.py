"""Local durable outbox for a single immutable caused-stop decision. [daveey]

This is not sync authority: only authenticated Board receipt readback settles it.
Keep completed requests too, so no later invocation can replace their bytes.
A capture_pending record is only an immutable validated semantic decision/UID,
not a serialized transport envelope. It has never been submitted. Its one legal
transition captures epochs once under the native writer and removes that marker;
existing captured or legacy requests never acquire a newer epoch. The semantic
decision names a prerequisite task, not a particular administrative home: first
capture may follow that task's completed transfer, with current source/stop/hold
validation and Board admission still required. It never rebinds a prior packet.
Outbound rejects
the marker. Frozen prerequisite waits consume no model/reconciliation attempts;
ordinary submission/reconciliation retains the existing bounded attempt budget.
"""
from __future__ import annotations

import json

from hermes_cli import kanban_spend_review_deferred as deferred_store

TABLE = "spend_review_requests"
# Separate from judge retries: allow receipt recovery after the model budget ends,
# but never monopolize reviewer custody indefinitely when no receipt is reachable.
MAX_RECONCILE_ATTEMPTS = 4
OPEN_INDEX = f"CREATE INDEX IF NOT EXISTS {TABLE}_open ON {TABLE}(task, episode) WHERE closed = 0"


def exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def requests(conn):
    if not exists(conn):
        return []
    return [json.loads(row[0]) for row in conn.execute(f"SELECT request FROM {TABLE}")]


def _has_closed(conn):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    return None if row is None else "closed INTEGER" in (row[0] or "")


def open_keys(conn):
    """(task, episode) of requests whose review is not yet known terminal. One indexed read whose cost does not
    grow with closed history; a table from before the marker reads as all open until the first close."""
    marked = _has_closed(conn)
    if marked is None:
        return []
    where = " WHERE closed = 0" if marked else ""
    return list(dict.fromkeys([(row[0], row[1]) for row in conn.execute(f"SELECT task, episode FROM {TABLE}{where}")]
                              + deferred_store.open_keys(conn)))


def close(conn, keys):
    """Mark requests whose review ended so later ticks skip them. The frozen bytes are kept unchanged."""
    from hermes_cli import kanban_db as kb
    if not keys:
        return
    with kb.write_txn(conn):
        from hermes_cli.kanban_home_transfer import frozen
        keys = [key for key in keys if not frozen(conn, key[0])]
        if not keys:
            return
        if _has_closed(conn) is False:
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN closed INTEGER NOT NULL DEFAULT 0")
            conn.execute(OPEN_INDEX)
        conn.executemany(f"UPDATE {TABLE} SET closed = 1 WHERE task = ? AND episode = ?", list(keys))
        deferred_store.close(conn, list(keys))


def get(conn, task, episode):
    if not exists(conn):
        return None
    row = conn.execute(f"SELECT request FROM {TABLE} WHERE task=? AND episode=?", (task, episode)).fetchone()
    return json.loads(row[0]) if row else deferred_store.get(conn, task, episode)


def freeze(conn, board, gateway, wire):
    from hermes_cli import kanban_db as kb
    # Network submission must never precede the outermost durable COMMIT.
    if conn.in_transaction:
        raise ValueError("cannot freeze a review inside another transaction")
    with kb.write_txn(conn):
        prior = get(conn, wire["task"], wire["episode"])
        if prior is not None:
            return prior  # Immutable legacy/current wire; never enrich on replay.
        from hermes_cli import kanban_home_command as home
        from hermes_cli.kanban_home_transfer import frozen as home_frozen
        if home_frozen(conn, wire['task']):
            raise home.Pending('home transfer pending')
        # Retain the validated decision/UID even when its prerequisite cannot
        # yet be captured. This is NOT a transport request and cannot be sent.
        deferred = False
        try:
            epoch = home.capture(conn, {**wire, "parent": wire.get("target")})
        except home.Pending:
            epoch, deferred = None, True
        conn.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (task TEXT NOT NULL, episode TEXT NOT NULL, "
                     "request TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(task,episode))")
        if _has_closed(conn):
            conn.execute(OPEN_INDEX)
        request = {"board": board, "gateway": gateway, "wire": wire, "home_generations": epoch}
        if deferred:
            request['capture_pending'] = True
            deferred_store.save(conn, wire['task'], wire['episode'], request)
        else:
            conn.execute(f"INSERT OR IGNORE INTO {TABLE} (task, episode, request) VALUES (?,?,?)",
                         (wire["task"], wire["episode"], json.dumps(request, ensure_ascii=False)))
        frozen = get(conn, wire["task"], wire["episode"])
        assert frozen is not None
        return frozen


def capture_deferred(conn, task, episode):
    """Capture a never-submitted decision once; old/legacy envelopes never change."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_home_command as home
    if conn.in_transaction:
        raise ValueError('cannot capture a review inside another transaction')
    with kb.write_txn(conn):
        request = get(conn, task, episode)
        if not request or not request.get('capture_pending'):
            return request
        wire = request['wire']
        epoch = home.capture(conn, {**wire, 'parent': wire.get('target')})
        request = {k: v for k, v in request.items() if k != 'capture_pending'}
        request['home_generations'] = epoch
        conn.execute(f'UPDATE {deferred_store.TABLE} SET captured=1,closed=1 WHERE task_id=? AND episode=?', (task, episode))
        conn.execute(f'INSERT INTO {TABLE}(task,episode,request) VALUES (?,?,?)',
                     (task, episode, json.dumps(request, ensure_ascii=False)))
        return request


def waiting_for_custody(conn, request):
    """No reconciliation attempt is spent while a never-sent target is frozen."""
    from hermes_cli.kanban_home_transfer import frozen
    return bool(request and request.get('capture_pending')
                and frozen(conn, request['wire']['target']))


def escalate_unconfirmed(conn, task, episode):
    """Transfer exhausted automation to an administrative owner, never cancel it.

    Keep the frozen bytes and all execution holds. This also covers terminal or
    superseded source cards, for which ordinary stop escalation is a no-op.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_spend_reviewer as reviewer
    with kb.write_txn(conn):
        from hermes_cli.kanban_home_transfer import frozen
        if frozen(conn, task):
            return
        request = get(conn, task, episode)
        if request is None or reviewer.state(conn, task, episode) not in (reviewer.QUEUED, reviewer.STARTED):
            return
        uid = request["wire"]["event_uid"]
        source = kb.get_task(conn, task)
        reason = "Frozen spend review remains unconfirmed after bounded reconciliation"
        body = (f"{reason}. Source card: {task}; stop: {episode}; request: {uid}; "
                f"Board: {request['board']}; gateway: {request['gateway']}.\n\n"
                "Automation has stopped; this is not cancellation or proof that the request was never submitted. "
                "An absent receipt can still race an in-flight operation. The immutable request journal is retained.\n"
                "Reconcile the exact authenticated Board receipt and report its disposition to the owner. "
                "Do not resubmit, choose a replacement verdict, grant runs, or promote the source card. "
                "Preserve human, terminal, scope and safety holds; safety refusals must never be retried. "
                "If no authoritative receipt is reachable, escalate to the Board administrator; do not infer clearance.")
        notice = kb.create_task(conn, title=f"Reconcile unconfirmed spend review on {task}", body=body,
                                assignee="default", created_by=reviewer.AUTHOR, workspace_kind="scratch",
                                tenant=source.tenant if source else None,
                                idempotency_key=f"spend-review-unconfirmed:{task}:{episode}")
        kb._append_event(conn, task, reviewer.ESCALATED,
                         {"episode": episode, "reason": reason, "task_id": notice,
                          "request_uid": uid, "unconfirmed": True})
        kb.add_comment(conn, task, reviewer.AUTHOR,
                       f"{reason}; administrative reconciliation: {notice}. "
                       "Request custody and execution holds retained; no cancellation or release inferred.", system=True)


def refuse(conn, task, episode, reason):
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        from hermes_cli.kanban_home_transfer import frozen
        if frozen(conn, task):
            return
        request = get(conn, task, episode)
        assert request is not None
        request["refused"] = reason
        conn.execute(f"UPDATE {TABLE} SET request=? WHERE task=? AND episode=?",
                     (json.dumps(request, ensure_ascii=False), task, episode))
