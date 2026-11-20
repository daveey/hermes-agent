"""Read-only view of a shadow-mode board's replica, merged under the hub-local rows. [daveey]

A board synced in ``shadow`` mode (``kanban_sync_state.mode``) keeps only this gateway's own cards in the live
``kanban.db``; the whole board (the mini's Baby Bear queue, say) lives in the replica
``kanban/sync-shadow/<board>.db``. Copying those rows into the live file is unsafe (primary mode partitions live
rows by gateway), so the read paths (``hermes kanban list`` / ``show`` and the dashboard ``/board``) merge them
in memory instead:

* the replica is opened read-only (:func:`kanban_foreign_card.open_replica`); nothing is written to it;
* a live row wins on an id collision;
* every merged row is marked ``remote`` / ``read_only`` and names its home gateway (``created_on``);
* the dispatcher, claims and the sync mode never see these rows: they read the live file only.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional

_log = logging.getLogger(__name__)

MODE = "shadow"
UNKNOWN_HOME = "unknown"


def is_shadow(conn: sqlite3.Connection) -> bool:
    """Whether the board behind the live connection ``conn`` syncs in shadow mode."""
    from hermes_cli import kanban_db_sync as sync
    try:
        state = sync.sync_state(conn)
    except sqlite3.Error:
        return False
    return bool(state) and state.get("mode") == MODE


@contextmanager
def open_shadow_replica(conn: sqlite3.Connection) -> Iterator[Optional[sqlite3.Connection]]:
    """A read-only connection to the replica of ``conn``'s board when it is a shadow board, else None."""
    from hermes_cli import kanban_foreign_card as foreign
    from hermes_cli.kanban_db_connect import _main_db_file
    if not is_shadow(conn):
        yield None
        return
    with foreign.open_replica(_main_db_file(conn)) as replica:
        yield replica


def db_file(conn: sqlite3.Connection) -> str:
    """The main database file behind ``conn`` ('' for an in-memory one)."""
    from hermes_cli.kanban_db_connect import _main_db_file
    return _main_db_file(conn) or ""


def home_of(replica: sqlite3.Connection, task_id: str) -> str:
    """The gateway that owns ``task_id`` (its ``created_on``), ``UNKNOWN_HOME`` when unrecorded."""
    from hermes_cli import kanban_foreign_card as foreign
    try:
        owner = foreign.created_on(replica, task_id)
    except sqlite3.Error:
        return UNKNOWN_HOME
    return UNKNOWN_HOME if owner is foreign._UNSET or owner is None else str(owner)


def open_card(live: sqlite3.Connection, scope: Any, task_id: str) -> tuple[Any, sqlite3.Connection, Optional[str]]:
    """``(task, connection, remote home)`` for ``hermes kanban show``: the live row, else a shadow board's replica
    row read through a read-only connection kept open in the ``ExitStack`` ``scope`` (home None for a live row).
    A card in neither is ``(None, live, None)``."""
    from hermes_cli import kanban_db as kb
    task = kb.get_task(live, task_id)
    if task:
        return task, live, None
    replica = scope.enter_context(open_shadow_replica(live))
    task = kb.get_task(replica, task_id) if replica is not None else None
    if not task:
        return None, live, None
    return task, replica, home_of(replica, task_id) or UNKNOWN_HOME


def _live_ids(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        found.update(r[0] for r in conn.execute(f"SELECT id FROM tasks WHERE id IN ({marks})", chunk))
    return found


def remote_tasks(conn: sqlite3.Connection, **filters: Any) -> list[tuple[Any, str]]:
    """``[(Task, home gateway)]``: the replica's cards matching ``filters`` (the ``list_tasks`` keywords) that the
    live file lacks. Empty unless ``conn``'s board is a shadow board with a readable replica."""
    from hermes_cli import kanban_db as kb
    with open_shadow_replica(conn) as replica:
        if replica is None:
            return []
        try:
            tasks = kb.list_tasks(replica, **filters)
            live = _live_ids(conn, [t.id for t in tasks])
            return [(t, home_of(replica, t.id)) for t in tasks if t.id not in live]
        except sqlite3.Error as exc:  # an unreadable replica adds nothing; the live rows still list
            _log.info("kanban: shadow replica read failed (%s)", exc)
            return []


def mark(d: dict[str, Any], home: Optional[str]) -> dict[str, Any]:
    """``d`` (a task dict) stamped as a remote, read-only row owned by ``home`` (``unknown`` when the replica row
    records no owner). ``home`` None is a live row (``show``): ``d`` comes back unchanged."""
    if home is None:
        return d
    return {**d, "remote": True, "read_only": True, "home_gateway": home}


def show_details(conn: sqlite3.Connection, task: Any, board: Optional[str], home: Optional[str]) -> dict[str, Any]:
    """``show``'s queue/capacity fields. A remote card has no queue here: it waits on its home gateway."""
    if home is not None:
        return {"capacity_note": f"remote (read-only replica of {home})"}
    from hermes_cli import kanban_queue_position as kqp
    return kqp.detail_fields(conn, task, board)


def print_remote(field: Any, home: Optional[str]) -> None:
    if home is not None:
        field("remote", f"read-only copy; home gateway {home}")


