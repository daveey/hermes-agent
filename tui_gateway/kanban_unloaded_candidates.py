"""Published-input candidates for the unloaded sweeper, not delivery decisions."""
from copy import deepcopy
from pathlib import Path
import sqlite3


def subscriptions(conn: sqlite3.Connection, kinds: tuple[str, ...]) -> list[dict]:
    """Reuse structural rows across reopened sweep connections.

    A retained, generation-guarded observer sees WAL commits via data_version.
    Transactions use the caller directly. Positive candidates always reach the
    normal event/grace/holder checks; no clock or process result is memoized.
    """
    from hermes_cli.kanban_reader_retention import reader

    if not conn.in_transaction:
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        if path:
            try:
                with reader(Path(path).resolve()) as observer:
                    return _on_connection(observer, kinds)
            except (sqlite3.Error, OSError):
                pass  # optimization failure must not suppress the caller's scan
    return _on_connection(conn, kinds)


def _on_connection(conn, kinds):
    stamp = None
    if not conn.in_transaction and hasattr(conn, '__dict__'):
        stamp = (conn.execute('PRAGMA data_version').fetchone()[0], conn.total_changes, kinds)
    memo = getattr(conn, '_unloaded_subscriptions', None)
    if stamp is not None and memo is not None and memo[0] == stamp:
        return deepcopy(memo[1])
    rows = _read_candidates(conn, kinds)
    if stamp is not None:
        conn._unloaded_subscriptions = (stamp, rows)
    return deepcopy(rows)


def _read_candidates(conn, kinds):
    from hermes_cli.kanban_db_notify import _decode_notify_delivery_metadata

    # Preserve in-flight rows as a conservative superset. Dead-holder recovery
    # runs before this query; downstream live-holder behavior stays unchanged.
    kind_filter = f"AND e.kind IN ({','.join('?' * len(kinds))})" if kinds else ""
    sql = ("SELECT s.* FROM kanban_notify_subs s WHERE LOWER(s.platform) = 'tui' "
           "AND s.chat_id != '' AND (s.inflight_from IS NOT NULL OR EXISTS "
           "(SELECT 1 FROM task_events e WHERE e.task_id=s.task_id AND e.id>s.last_event_id "
           f"{kind_filter}))")
    cursor = conn.execute(sql, kinds)
    columns = [column[0] for column in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    for row in rows:
        if 'delivery_metadata' in row:
            row['delivery_metadata'] = _decode_notify_delivery_metadata(row['delivery_metadata'])
    return rows
