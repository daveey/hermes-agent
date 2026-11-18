"""Local containment backstop; ingress authorization is deliberately separate.

Registered by normal connections as a fail-closed storage fence, not an API
for containing or resuming a card. Release verification is supplied by the
canonical ingress owner, never inferred from generic lifecycle events.
"""
from __future__ import annotations

import contextlib
import contextvars
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from hermes_cli.kanban_containment_state import reduce_events

_WRITE: contextvars.ContextVar[tuple[int, str] | None] = contextvars.ContextVar("containment_write", default=None)
_DELETE: contextvars.ContextVar[tuple[int, str] | None] = contextvars.ContextVar("containment_delete", default=None)


@contextlib.contextmanager
def authorized_write(conn: sqlite3.Connection, task_id: str) -> Iterator[None]:
    """Internal writer seam AFTER scoped verification; requires an active txn.

    Only task-state changes and new containment receipts use this seam. It does
    not permit deleting history, removing edges or completing held dependents.
    """
    if not conn.in_transaction:
        raise ValueError("containment writer requires an active transaction")
    token = _WRITE.set((id(conn), task_id))
    try:
        yield
    finally:
        _WRITE.reset(token)


@contextlib.contextmanager
def released_history_delete(conn: sqlite3.Connection, task_id: str, *, verify_release: Callable | None = None) -> Iterator[None]:
    """Scoped custody for deleting a task's protected history with the task.

    Refuses (containment_pending) while the task is held for any reason, so
    active/unverified/unobserved containment keeps its history. Once released
    and verified, the history goes away only together with its task.
    """
    if pending(conn, task_id, verify_release=verify_release):
        raise sqlite3.IntegrityError("containment_pending")
    token = _DELETE.set((id(conn), task_id))
    try:
        yield
    finally:
        _DELETE.reset(token)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='containment_observations'").fetchone():
        conn.execute("DELETE FROM containment_observations WHERE task_id=?", (task_id,))


def inert_snapshot(conn: sqlite3.Connection) -> None:
    """Private snapshot copies only: stub fence functions so raw scrubs can run.

    Never use on a live board connection; history rows are preserved so the
    importing board re-derives the hold from the events themselves.
    """
    for name, arity in (("kanban_containment_held", 1), ("kanban_containment_write", 1), ("kanban_containment_delete", 1),
                        ("kanban_containment_sync_task", 1), ("kanban_containment_sync_event", 4),
                        ("kanban_containment_sync_edge", 2), ("kanban_containment_cache_write", 0)):
        conn.create_function(name, arity, lambda *a: 0)


def pending(conn: sqlite3.Connection, task_id: str, *, verify_release: Callable | None = None) -> bool:
    """Read only; old no-UID histories remain held rather than inventing identity."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(task_events)")}
    uid = "uid" if "uid" in columns else "NULL AS uid"
    cursor = conn.execute(f"SELECT {uid},task_id,kind,payload FROM task_events WHERE task_id=? "
                          "AND kind IN ('contained','containment_released')", (task_id,))
    names = [column[0] for column in cursor.description]
    events = [dict(zip(names, row)) for row in cursor]
    # A canonical task page may precede its event page. Seeing a marker grants
    # only a hold; clearing it later cannot substitute for the missing receipt.
    observed = set()
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='containment_observations'").fetchone():
        observed.update(r[0] for r in conn.execute("SELECT episode FROM containment_observations WHERE task_id=?", (task_id,)))
    if "containment_episode" in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:
        marker = conn.execute("SELECT containment_episode FROM tasks WHERE id=?", (task_id,)).fetchone()
        if marker and marker[0] is not None:
            observed.add(marker[0])
    known = {event["uid"] for event in events if event["kind"] == "contained"}
    from hermes_cli.kanban_containment_requests import pending as request_pending
    if request_pending(conn, task_id, known):
        return True
    if observed - known:
        return True
    return reduce_events(events, verify_release=verify_release) is not None


def register(conn: sqlite3.Connection, *, verify_release: Callable | None = None) -> None:
    """Register per-connection functions without schema writes or network I/O."""
    conn.create_function("kanban_containment_held", 1,
                         lambda task: int(pending(conn, task, verify_release=verify_release)))
    conn.create_function("kanban_containment_delete", 1,
                         lambda task: int(conn.in_transaction and _DELETE.get() == (id(conn), task)))
    conn.create_function("kanban_containment_write", 1,
                         lambda task: int(conn.in_transaction and _WRITE.get() == (id(conn), task)))
    from hermes_cli import kanban_containment_replica as replica
    conn.create_function("kanban_containment_sync_task", 1, lambda task: replica.task_permit(conn, task))
    conn.create_function("kanban_containment_sync_event", 4,
                         lambda uid, task, kind, payload: replica.event_permit(conn, uid, task, kind, payload))
    conn.create_function("kanban_containment_sync_edge", 2, lambda parent, child: replica.edge_permit(conn, parent, child))
    replica.register(conn)


def install(conn: sqlite3.Connection, *, verify_release: Callable[
    [Mapping[str, Any], Mapping[str, Any]], bool] | None = None,
) -> None:
    """Register this connection's read/permit functions and idempotent triggers.

    Every writer connection must register the functions before opening a fenced
    DB. A connection without them fails closed at SQLite, not open on restart.
    """
    register(conn, verify_release=verify_release)
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn, allow_nested=True):
        _install_schema(conn)


def _install_schema(conn: sqlite3.Connection) -> None:
    # Upgrade all triggers atomically: failure must retain the previous fence.
    conn.execute("CREATE TABLE IF NOT EXISTS containment_observations (task_id TEXT NOT NULL,episode TEXT NOT NULL,"
                 "PRIMARY KEY(task_id,episode))")
    conn.execute("INSERT OR IGNORE INTO containment_observations SELECT id,containment_episode FROM tasks "
                 "WHERE containment_episode IS NOT NULL")
    for operation in ("INSERT", "UPDATE OF containment_episode"):
        name = operation.split()[0].lower()
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS containment_observe_{name} AFTER {operation} ON tasks "
                     "WHEN NEW.containment_episode IS NOT NULL BEGIN INSERT OR IGNORE INTO containment_observations "
                     "VALUES (NEW.id,NEW.containment_episode); END")
    _triggers(conn)


def _triggers(conn: sqlite3.Connection) -> None:
    held = "kanban_containment_held"
    permit = "kanban_containment_write"
    fail = "SELECT RAISE(ABORT,'containment_pending');"
    protected = "('contained','containment_released')"
    columns = [r[1] for r in conn.execute("PRAGMA table_info(task_events)")]
    new_uid = "NEW.uid" if "uid" in columns else "NULL"
    statements = {
        "task_update": ("BEFORE UPDATE OF status,current_run_id,completed_at,containment_episode ON tasks",
                        f"{held}(OLD.id) AND (OLD.status IS NOT NEW.status OR OLD.current_run_id IS NOT NEW.current_run_id "
                        "OR OLD.completed_at IS NOT NEW.completed_at OR OLD.containment_episode IS NOT NEW.containment_episode) "
                        "AND NOT kanban_containment_sync_task(OLD.id) AND ("
                        f"NOT {permit}(OLD.id) OR NEW.status != 'triage' "
                        "OR NEW.current_run_id IS NOT NULL OR NEW.completed_at IS NOT NULL)"),
        "task_delete": ("BEFORE DELETE ON tasks", f"{held}(OLD.id)"),
        "event_insert": ("BEFORE INSERT ON task_events",
                         f"NEW.kind IN {protected} AND NOT {permit}(NEW.task_id) "
                         f"AND NOT kanban_containment_sync_event({new_uid},NEW.task_id,NEW.kind,NEW.payload)"),
        "event_delete": ("BEFORE DELETE ON task_events", f"OLD.kind IN {protected} AND NOT kanban_containment_delete(OLD.task_id)"),
    }
    same = " AND ".join(f'OLD."{c}" IS NEW."{c}"' for c in columns if c != "uid")
    uid_fill = (f"OLD.uid IS NULL AND NEW.uid IS NOT NULL AND ({same})" if "uid" in columns else "0")
    replica_same = ("OLD.uid IS NEW.uid AND OLD.task_id IS NEW.task_id AND OLD.kind IS NEW.kind "
                    "AND OLD.payload IS NEW.payload AND kanban_containment_sync_event(NEW.uid,NEW.task_id,NEW.kind,NEW.payload)"
                    if "uid" in columns else "0")
    statements["event_update"] = ("BEFORE UPDATE ON task_events",
                                  f"(OLD.kind IN {protected} OR NEW.kind IN {protected}) AND NOT ({uid_fill}) "
                                  f"AND NOT ({replica_same})")
    for op, refs in (("insert", ("NEW",)), ("delete", ("OLD",)), ("update", ("OLD", "NEW"))):
        condition = " OR ".join(f"{held}({ref}.{end}_id)" for ref in refs for end in ("parent", "child"))
        if op == "insert":
            condition = f"({condition}) AND NOT kanban_containment_sync_edge(NEW.parent_id,NEW.child_id)"
        statements[f"edge_{op}"] = (f"BEFORE {op.upper()} ON task_links", condition)
    for name, (operation, condition) in statements.items():
        conn.execute(f"DROP TRIGGER IF EXISTS containment_{name}")
        conn.execute(f"CREATE TRIGGER containment_{name} {operation} WHEN {condition} BEGIN {fail} END")
