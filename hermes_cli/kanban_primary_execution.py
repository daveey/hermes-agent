"""Executor-owned running evidence on a PRIMARY mirror; never local read authority."""
from hermes_cli.kanban_replica_execution import LIVENESS_KINDS, raw, run_evidence
from hermes_cli.kanban_table_columns import tables_columns


class PrimaryMirror(ValueError):
    """The persisted PRIMARY mode refuses local authority, but permits replica evidence."""


def attach(conn, rows, *, gateway, profile, board):
    """Attach only qualified current-run evidence, in the caller's read transaction.

    Integer run IDs are local joins, not portable identities. Use the matched running
    attempt's UID and executor event; task timestamps, pull time and read time attest nothing.
    Missing old-schema columns leave the ordinary unknown path intact.
    """
    return attach_materialized(materialize(conn, rows), rows, gateway=gateway, profile=profile, board=board)


def materialize(conn, rows):
    """Fetch current-run tuples; the returned list owns no cursor or connection."""
    tasks, runs, events = tables_columns(conn, 'tasks', 'task_runs', 'task_events')  # one row, not one per column
    if not ({'id', 'status', 'current_run_id', 'assignee'} <= tasks
            and {'id', 'task_id', 'uid', 'status', 'ended_at'} <= runs
            and {'task_id', 'run_id', 'kind', 'created_at', 'uid'} <= events):
        return []
    values = []
    ids = list(rows)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        query = (
            'SELECT t.id, r.uid, (SELECT json_array(e.created_at, e.uid) FROM task_events e '
            'WHERE e.run_id=t.current_run_id AND e.task_id=t.id AND e.kind IN (?, ?, ?) '
            'ORDER BY e.created_at DESC, e.uid DESC LIMIT 1), t.assignee FROM tasks t JOIN task_runs r '
            'ON r.id=t.current_run_id AND r.task_id=t.id '
            "WHERE t.status='running' AND r.status='running' AND r.ended_at IS NULL "
            f"AND t.id IN ({','.join('?' for _ in chunk)})")
        values.extend(conn.execute(query, (*LIVENESS_KINDS, *chunk)).fetchall())
    return values


def attach_materialized(values, rows, *, gateway, profile, board):
    """Derive executor evidence after the source transaction has closed."""
    out = {}
    for tid, uid, beat, assignee in values:
        evidence = run_evidence({'status': 'running', 'run_uid': uid, 'run_beat': beat})
        if evidence is None:
            continue
        row = rows[tid]
        row.update(board=board, status='running', assignee=assignee)
        from hermes_cli.kanban_execution_observations import _request
        row['human_request'] = _request(row.get('human_request'))
        # Evidence ownership is private adapter data, not remote-card UI routing.
        row['_replica_run'] = {**evidence, 'activity_at': None, 'activity_kind': None,
                               'scope': {'gateway': gateway, 'owner_profile': profile}}
        out[tid] = raw(row)
    return out
