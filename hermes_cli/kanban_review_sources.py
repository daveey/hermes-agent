"""Read related review sources from the selected board's synced replica. [daveey]"""
from contextlib import contextmanager
import sqlite3


@contextmanager
def source_reader(conn):
    """Local evidence wins; absent cards use one read-only replica snapshot.

    Pickup intentionally imports runnable cards, not their terminal creators. Do
    not import creators for routing: eviction would remove them again, and a
    runnable source must not gain execution custody merely by being read.
    Runs and their events always use the same connection (integer IDs are local).
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli.kanban_review_model import ReviewModelUnavailable

    replica = None

    def lookup(task_id):
        nonlocal replica
        local = kb.get_task(conn, task_id)
        if local is not None:
            return conn, local
        state = sync.sync_state(conn) or {}
        if state.get('mode') == 'primary':
            if replica is None:
                replica = wn.open_replica(conn)
                if replica is not None:
                    replica.execute('BEGIN')
            if replica is not None:
                remote = kb.get_task(replica, task_id)
                if remote is not None:
                    return replica, remote
        raise ReviewModelUnavailable(f'Review implementer evidence is unavailable for source {task_id}')

    try:
        yield lookup
    except (sqlite3.Error, OSError) as exc:
        raise ReviewModelUnavailable(f'Review implementer evidence is unreadable: {exc}') from exc
    finally:
        if replica is not None:
            replica.close()
