"""Snapshot-local graph inputs for the parked/held annotation walk."""
import json


def open_parent_edges(conn, unfinished):
    """Read only edges the walk can follow; never fetch the historical graph.

    ``unfinished`` is the caller's already-read task-status map. Passing its IDs
    avoids scanning tasks again and keeps parent selection on that same view.
    No memo or connection survives this pass. Missing legacy tables still raise
    to the caller's existing compatibility handler.
    """
    parents = {}
    for parent, child in conn.execute(
            'SELECT parent_id, child_id FROM task_links '
            'WHERE parent_id IN (SELECT value FROM json_each(?))', (json.dumps(list(unfinished)),)):
        parents.setdefault(str(child), []).append(str(parent))
    return parents
