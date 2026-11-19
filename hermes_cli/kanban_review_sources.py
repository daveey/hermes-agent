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

    ``lookup.children(task_id)`` lists a card's linked child ids from the live file and, on a synced board, the
    replica: pickup copies only a card's parent edges, and the feed keeps only links whose child is local, so a
    picked-up review card's links to its sources live only in the replica (worker refusals 2026-11-19). [daveey]
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli.kanban_review_model import ReviewModelUnavailable

    replica = None
    opened = False

    def board_replica():
        nonlocal replica, opened
        if not opened and (sync.sync_state(conn) or {}).get('mode') == 'primary':
            opened = True
            replica = wn.open_replica(conn)
            if replica is not None:
                replica.execute('BEGIN')
        return replica

    def lookup(task_id):
        local = kb.get_task(conn, task_id)
        if local is not None:
            return conn, local
        remote_board = board_replica()
        if remote_board is not None:
            remote = kb.get_task(remote_board, task_id)
            if remote is not None:
                return remote_board, remote
        raise ReviewModelUnavailable(f'Review implementer evidence is unavailable for source {task_id}')

    def children(task_id):
        query = 'SELECT child_id FROM task_links WHERE parent_id=? ORDER BY child_id'
        found = [row[0] for row in conn.execute(query, (task_id,)).fetchall()]
        remote_board = board_replica()
        if remote_board is not None:
            found += [row[0] for row in remote_board.execute(query, (task_id,)).fetchall() if row[0] not in found]
        return found

    lookup.children = children
    try:
        yield lookup
    except (sqlite3.Error, OSError) as exc:
        raise ReviewModelUnavailable(f'Review implementer evidence is unreadable: {exc}') from exc
    finally:
        if replica is not None:
            replica.close()
