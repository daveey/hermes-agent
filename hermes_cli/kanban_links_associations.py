"""Association-only local read for receipt dedup under the links projection lock."""
from contextlib import closing
import sqlite3


def read(path, ids, statuses):
    """Preserve listed creator/subscriber selection, without execution, waits or history.

    An owned reader never queues behind a sidebar borrower. The caller retains
    bounded busy fallback in its separate memo namespace; no transaction escapes.
    """
    from hermes_cli.kanban_session_activity import _execution_transaction, _open_ro
    from hermes_cli.kanban_session_outcome_tasks import select_listed_local_tasks

    with closing(_open_ro(path, timeout=0.5)) as conn, _execution_transaction(conn):
        conn.row_factory = sqlite3.Row
        return list(select_listed_local_tasks(conn, ids, statuses).values())


def pairs(path, ids, statuses):
    """Every listed ``(session, task)`` association of ``ids`` on ``path`` in one read: ``[{"session", "id"}]``.

    [daveey] The receipt dedup asked ``read(path, [sid])`` once per alias, each through its own guarded connection:
    a third of every links projection on the hub (2026-11-23), all of it under ``_links_lock``. A session is
    associated with a task here exactly when ``read(path, [session])`` would list it (creator or counted
    subscriber, listed status, not a watch). Same connection ownership and optional-subscriptions boundary."""
    from hermes_cli.kanban_session_activity import _execution_transaction, _open_ro
    from hermes_cli.kanban_session_outcome_tasks import select_listed_local_task_set

    with closing(_open_ro(path, timeout=0.5)) as conn, _execution_transaction(conn):
        conn.row_factory = sqlite3.Row
        _found, members = select_listed_local_task_set(conn, ids, statuses, strict=False)
        return [{"session": sid, "id": tid} for sid, tid in members]
