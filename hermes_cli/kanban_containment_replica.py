"""Authenticated receipt cache and narrowly scoped replica projection.

The cache is host-local, never synced authority. Only fresh authenticated Board
/evidence responses populate it; no networking takes place in SQLite functions.
The sync flag alone grants no permission to insert protected history.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import sqlite3
from collections.abc import Iterator

from hermes_cli import kanban_containment_canonical as canonical
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from hermes_cli.kanban_containment_state import _payload

_CACHE = contextvars.ContextVar("containment_cache_write", default=None)
_SCOPE = contextvars.ContextVar("containment_replica_projection", default=None)


def register(conn: sqlite3.Connection) -> None:
    conn.create_function("kanban_containment_cache_write", 0, lambda: int(_CACHE.get() == id(conn) and conn.in_transaction))


def install(conn: sqlite3.Connection) -> None:
    register(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS containment_canonical_receipts ("
                 "uid TEXT PRIMARY KEY,board TEXT NOT NULL,task_id TEXT NOT NULL,kind TEXT NOT NULL,"
                 "payload TEXT NOT NULL,origin TEXT NOT NULL,evidence TEXT NOT NULL)")
    for op in ("UPDATE", "DELETE"):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS containment_cache_{op.lower()} BEFORE {op} "
                     "ON containment_canonical_receipts BEGIN SELECT RAISE(ABORT,'containment_pending'); END")
    conn.execute("CREATE TRIGGER IF NOT EXISTS containment_cache_insert BEFORE INSERT ON containment_canonical_receipts "
                 "WHEN NOT kanban_containment_cache_write() BEGIN SELECT RAISE(ABORT,'containment_pending'); END")


def remember(conn: sqlite3.Connection, evidence: dict, *, board: str, task: str, uid: str,
             stop_evidence: dict | None = None) -> None:
    """Trusted transport seam: caller fetched evidence directly from enrolled Hub.

    A replica's event or request-supplied origin/digest is not an input to this seam.
    Code with the enrolled key remains trusted; this is not an OS sandbox.
    """
    if _board(conn) != board:
        raise ContainmentRefused("canonical receipt belongs to a different board")
    if stop_evidence is None:
        row = canonical.certified_stop(evidence, board=board, task=task, uid=uid)
    else:
        episode = stop_evidence.get("uid")
        _, row = canonical.certified_release(stop_evidence, evidence, board=board, task=task,
                                              episode=episode, receipt=uid)
        remember(conn, stop_evidence, board=board, task=task, uid=episode)
    install(conn)
    raw = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
    values = (uid, board, task, row["kind"], row["payload"], row["origin_gateway"], raw)
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn, allow_nested=True):
        prior = conn.execute("SELECT uid,board,task_id,kind,payload,origin,evidence FROM containment_canonical_receipts "
                             "WHERE uid=?", (uid,)).fetchone()
        if prior is not None:
            if tuple(prior) != values:
                raise ContainmentRefused("canonical receipt identity changed")
            return
        token = _CACHE.set(id(conn))
        try:
            conn.execute("INSERT INTO containment_canonical_receipts VALUES (?,?,?,?,?,?,?)", values)
        finally:
            _CACHE.reset(token)


def _cached(conn: sqlite3.Connection, uid: str) -> dict | None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='containment_canonical_receipts'").fetchone():
        return None
    row = conn.execute("SELECT evidence FROM containment_canonical_receipts WHERE uid=?", (uid,)).fetchone()
    return json.loads(row[0]) if row else None


def _matches(event: dict, receipt: dict) -> bool:
    row = receipt["row"]
    return (event.get("uid") == row["uid"] and event.get("task_id") == row["task_id"]
            and event.get("kind") == row["kind"] and _payload(event) == _payload(row))


def _board(conn: sqlite3.Connection) -> str | None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_sync_state'").fetchone():
        return None
    row = conn.execute("SELECT board_id FROM kanban_sync_state LIMIT 1").fetchone()
    return row[0] if row else None


def verified(conn: sqlite3.Connection, release: dict, stop: dict) -> bool:
    s, r = _cached(conn, stop.get("uid")), _cached(conn, release.get("uid"))
    if s is None or r is None or s["board"] != r["board"] or s["board"] != _board(conn) or not _matches(stop, s) or not _matches(release, r):
        return False
    try:
        canonical.certified_release(s, r, board=s["board"], task=stop["task_id"],
                                    episode=stop["uid"], receipt=release["uid"])
    except ContainmentRefused:
        return False
    return True


def certify_page(conn: sqlite3.Connection, hub, board: str, changes) -> None:
    """Fetch before the writer transaction. Unavailable proof aborts the page.

    A task marker may arrive first; that grants only a hold and needs no release
    receipt. Stop/release events must match exact canonical bytes and origin.
    """
    for ch in changes:
        if ch.table != "task_events" or ch.fields.get("kind") not in ("contained", "containment_released"):
            continue
        if ch.op != "upsert":
            raise ContainmentRefused("protected containment history cannot be deleted")
        proof = hub.row_evidence(board, "task_events", ch.uid)
        stop = None
        if ch.fields["kind"] == "containment_released":
            data = _payload(ch.fields)
            if data is None:
                raise ContainmentRefused("invalid canonical release page")
            stop = hub.row_evidence(board, "task_events", data.get("episode"))
        remember(conn, proof, board=board, task=ch.fields.get("task_id"), uid=ch.uid, stop_evidence=stop)
        if not _matches({**ch.fields, "uid": ch.uid}, proof) or ch.origin_gateway != proof["row"]["origin_gateway"]:
            raise ContainmentRefused("replica event differs from its canonical receipt")


@contextlib.contextmanager
def projection(conn: sqlite3.Connection, change) -> Iterator[None]:
    """Internal apply scope: one connection, transaction and exact wire row only."""
    if not conn.in_transaction:
        raise ContainmentRefused("replica containment projection requires a transaction")
    token = _SCOPE.set((id(conn), change))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def task_permit(conn: sqlite3.Connection, task: str) -> int:
    scope = _SCOPE.get()
    return int(bool(conn.in_transaction and scope and scope[0] == id(conn)
                    and scope[1].table == "tasks" and scope[1].op == "upsert" and scope[1].uid == task))


def prepare_apply(conn: sqlite3.Connection, changes) -> None:
    relevant = any((ch.table == "tasks" and ch.fields.get("containment_episode") is not None)
                   or (ch.table == "task_events" and ch.fields.get("kind") in ("contained", "containment_released"))
                   for ch in changes)
    if relevant or conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='containment_observations'").fetchone():
        from hermes_cli import kanban_containment_guard as guard
        from hermes_cli.kanban_containment_connections import verifier
        guard.install(conn, verify_release=lambda r, s: verifier(conn, r, s))
        for ch in changes:
            if ch.table == "task_events" and ch.fields.get("kind") == "containment_released":
                proof = _cached(conn, ch.uid)
                if proof is not None:
                    data = _payload(proof["row"])
                    conn.execute("INSERT OR IGNORE INTO containment_observations(task_id,episode) VALUES (?,?)",
                                 (ch.fields.get("task_id"), data["episode"]))


def event_permit(conn: sqlite3.Connection, uid: str, task: str, kind: str, payload: str) -> int:
    scope = _SCOPE.get()
    if not conn.in_transaction or not scope or scope[0] != id(conn):
        return 0
    ch = scope[1]
    if ch.table != "task_events" or ch.uid != uid or ch.op != "upsert":
        return 0
    proof = _cached(conn, uid)
    event = {"uid": uid, "task_id": task, "kind": kind, "payload": payload}
    return int(bool(proof and proof["board"] == _board(conn) and _matches(event, proof) and _matches({**ch.fields, "uid": uid}, proof)
                    and ch.origin_gateway == proof["row"]["origin_gateway"]))


def edge_permit(conn: sqlite3.Connection, parent: str, child: str) -> int:
    scope = _SCOPE.get()
    return int(bool(conn.in_transaction and scope and scope[0] == id(conn)
                    and scope[1].table == "task_links" and scope[1].op == "upsert"
                    and scope[1].fields.get("parent_id") == parent and scope[1].fields.get("child_id") == child))


def restore_page(conn: sqlite3.Connection, changes) -> set[str]:
    """Restore held projections before commit; neither transient readiness nor stale outbox escapes."""
    from hermes_cli import kanban_containment_guard as guard
    from hermes_cli.kanban_containment_connections import held
    subjects = {ch.uid for ch in changes if ch.table == "tasks"}
    subjects.update(ch.fields.get("task_id") for ch in changes if ch.table == "task_events")
    retained = set()
    for task in subjects - {None}:
        if not held(conn, task):
            protected_seen = any(ch.table == "task_events" and ch.fields.get("task_id") == task
                                 and ch.fields.get("kind") in ("contained", "containment_released") for ch in changes)
            if protected_seen:
                _restore_released(conn, task)
            continue
        retained.add(task)
        with guard.authorized_write(conn, task):
            conn.execute("UPDATE tasks SET status='triage',current_run_id=NULL,completed_at=NULL,"
                         "claim_lock=NULL,claim_expires=NULL,worker_pid=NULL,worker_started_at=NULL WHERE id=?", (task,))
        # A pre-stop full task resend must not undo canonical containment. Keep
        # annotations and unrelated tables; discard only obsolete lifecycle fields.
        from hermes_cli.kanban_containment_outbox import strip_lifecycle
        strip_lifecycle(conn, task)
    return retained


def _restore_released(conn: sqlite3.Connection, task: str) -> None:
    # A release page may follow the cleared task page that we conservatively
    # re-held. Only an otherwise-unclaimed triage projection can be restored.
    row = conn.execute("SELECT status,current_run_id,human_request,block_kind FROM tasks WHERE id=?", (task,)).fetchone()
    if row is None or row[0] != "triage" or any(v is not None for v in row[1:]):
        return
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_spend_decision import pending as spend_pending
    landing = "triage" if spend_pending(conn, task) else kb._landing_status_after_parents(conn, task)
    conn.execute("UPDATE tasks SET status=?,containment_episode=NULL WHERE id=?", (landing, task))
