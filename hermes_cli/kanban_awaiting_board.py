"""Read-only board-qualified producer lookup, separate from the waiter's write pin."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3


def other_board_path(conn, awaiting):
    if not awaiting or awaiting.get("kind", "card") != "card" or awaiting.get("gateway") or not awaiting.get("board"):
        return None
    from hermes_cli.kanban_db import board_own_db_path, _normalize_board_slug
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.kanban_db_sync import board_slug_for_path
    from hermes_cli.kanban_awaiting import AwaitingError, INVALID_AWAITING
    try:
        path = board_own_db_path(awaiting["board"])
    except ValueError as exc:
        raise AwaitingError(INVALID_AWAITING, str(exc)) from exc
    current = _main_db_file(conn)
    if current and _normalize_board_slug(awaiting["board"]) == board_slug_for_path(current):
        return None
    return path


@contextmanager
def producer_connection(conn, awaiting):
    path = other_board_path(conn, awaiting)
    if path is None:
        yield conn
        return
    from hermes_cli.kanban_session_activity import _open_ro
    from hermes_cli.kanban_awaiting import AwaitingError, INVALID_AWAITING
    try:
        owner = _open_ro(Path(path))
    except (OSError, sqlite3.Error) as exc:
        raise AwaitingError(INVALID_AWAITING, f"cannot read producer board {awaiting['board']}: {exc}") from exc
    try:
        yield owner
    finally:
        owner.close()


def cross_board_cycle(conn, task_id, awaiting):
    """Follow qualified producer/parent edges; board-local IDs are not global identities."""
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.kanban_awaiting import _latest_park, _scheduled_probe_ids
    def identity(owner, ref):
        path = _main_db_file(owner)
        return (str(Path(path).resolve()) if path else str(id(owner)), ref)

    origin = identity(conn, task_id)
    seen = set()

    def visit(owner, ref):
        key = identity(owner, ref)
        if key == origin:
            return f"waiting on {awaiting['ref']} cycles back to {task_id}"
        if key in seen:
            return None
        if len(seen) >= 32:
            return "producer dependency graph exceeds the bounded cycle check"
        seen.add(key)
        row = owner.execute("SELECT status FROM tasks WHERE id=?", (ref,)).fetchone()
        if not row:
            return None
        edges = []
        if row[0] == "todo":
            edges = [{"ref": p[0]} for p in owner.execute(
                "SELECT parent_id FROM task_links WHERE child_id=?", (ref,))]
        elif row[0] == "scheduled":
            record, _ = _latest_park(owner, ref)
            if record and record.get("kind") == "card" and not record.get("gateway"):
                edges.append(record)
            edges.extend({"ref": p} for p in _scheduled_probe_ids(owner, ref)
                         if not record or p != record.get("ref"))
        for edge in edges:
            with producer_connection(owner, edge) as next_owner:
                why = visit(next_owner, edge.get("ref", ""))
            if why:
                return why
        return None

    with producer_connection(conn, awaiting) as owner:
        return visit(owner, awaiting["ref"])
