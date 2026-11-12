"""Read-only historical dependency proof after executor partition eviction."""
from __future__ import annotations

import sqlite3
import time


def replica_historical_parent(conn, parent_id, parked_at):
    """Fresh same-board history, never a terminal status alone, can discharge history.

    This does not supply a review verdict or authorize a merge. The ordinary review
    checkpoint still binds the owner run, edges and PRs and is consumed on claim.
    Missing/old replicas and reopened parents retain the existing active-PR hold.
    """
    from hermes_cli.kanban_chat_links import _stale_after_s
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.kanban_dependency_review import _historical_parent_history
    from hermes_cli.kanban_foreign_card import open_replica

    try:
        local = conn.execute("SELECT board_id, mode FROM kanban_sync_state LIMIT 1").fetchone()
        if local is None or local["mode"] not in {"primary", "shadow"}:
            return False
        with open_replica(_main_db_file(conn)) as replica:
            if replica is None:
                return False
            # Pin stamp, task, run and events to one committed feed snapshot.
            replica.execute("BEGIN")
            state = replica.execute("SELECT board_id, pulled_at FROM kanban_sync_state LIMIT 1").fetchone()
            if state is None or state["board_id"] != local["board_id"]:
                return False
            pulled = state["pulled_at"]
            if (type(pulled) not in (int, float) or pulled < parked_at
                    or not 0 <= time.time() - pulled <= _stale_after_s()):
                return False
            return _historical_parent_history(replica, parent_id, parked_at)
    except (sqlite3.Error, OSError, ValueError):
        return False
