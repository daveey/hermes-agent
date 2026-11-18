"""Local containment backstop; ingress authorization is deliberately separate.

Not installed by normal connections until the lifecycle and sync capability
contract is complete. Installing it is a fail-closed storage fence, not an API
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


def pending(conn: sqlite3.Connection, task_id: str, *, verify_release: Callable | None = None) -> bool:
    """Read only; old no-UID histories remain held rather than inventing identity."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(task_events)")}
    uid = "uid" if "uid" in columns else "NULL AS uid"
    cursor = conn.execute(f"SELECT {uid},task_id,kind,payload FROM task_events WHERE task_id=? "
                          "AND kind IN ('contained','containment_released')", (task_id,))
    names = [column[0] for column in cursor.description]
    events = [dict(zip(names, row)) for row in cursor]
    return reduce_events(events, verify_release=verify_release) is not None


def install(conn: sqlite3.Connection, *, verify_release: Callable[
    [Mapping[str, Any], Mapping[str, Any]], bool] | None = None,
) -> None:
    """Register this connection's read/permit functions and idempotent triggers.

    Every writer connection must register the functions before opening a fenced
    DB. A connection without them fails closed at SQLite, not open on restart.
    """
    conn.create_function("kanban_containment_held", 1,
                         lambda task: int(pending(conn, task, verify_release=verify_release)))
    conn.create_function("kanban_containment_write", 1,
                         lambda task: int(conn.in_transaction and _WRITE.get() == (id(conn), task)))
    _triggers(conn)


def _triggers(conn: sqlite3.Connection) -> None:
    held = "kanban_containment_held"
    permit = "kanban_containment_write"
    fail = "SELECT RAISE(ABORT,'containment_pending');"
    protected = "('contained','containment_released')"
    statements = {
        "task_update": ("BEFORE UPDATE OF status,current_run_id,completed_at ON tasks",
                        f"{held}(OLD.id) AND (OLD.status IS NOT NEW.status OR OLD.current_run_id IS NOT NEW.current_run_id "
                        "OR OLD.completed_at IS NOT NEW.completed_at) AND ("
                        f"NOT {permit}(OLD.id) OR NEW.status != 'triage' "
                        "OR NEW.current_run_id IS NOT NULL OR NEW.completed_at IS NOT NULL)"),
        "task_delete": ("BEFORE DELETE ON tasks", f"{held}(OLD.id)"),
        "event_insert": ("BEFORE INSERT ON task_events",
                         f"NEW.kind IN {protected} AND NOT {permit}(NEW.task_id)"),
        "event_delete": ("BEFORE DELETE ON task_events", f"OLD.kind IN {protected}"),
    }
    columns = [r[1] for r in conn.execute("PRAGMA table_info(task_events)")]
    same = " AND ".join(f'OLD."{c}" IS NEW."{c}"' for c in columns if c != "uid")
    uid_fill = (f"OLD.uid IS NULL AND NEW.uid IS NOT NULL AND ({same})" if "uid" in columns else "0")
    statements["event_update"] = ("BEFORE UPDATE ON task_events",
                                  f"(OLD.kind IN {protected} OR NEW.kind IN {protected}) AND NOT ({uid_fill})")
    for op, refs in (("insert", ("NEW",)), ("delete", ("OLD",)), ("update", ("OLD", "NEW"))):
        condition = " OR ".join(f"{held}({ref}.{end}_id)" for ref in refs for end in ("parent", "child"))
        statements[f"edge_{op}"] = (f"BEFORE {op.upper()} ON task_links", condition)
    for name, (operation, condition) in statements.items():
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS containment_{name} {operation} WHEN {condition} BEGIN {fail} END")
