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
