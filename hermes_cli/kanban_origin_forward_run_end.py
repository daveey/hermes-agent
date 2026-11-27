"""Keep a queued forward's live admission lease until the courier settles it.

This is a voluntary-lifecycle gate, not an authorization source. Only the origin
can freeze admission; courier bodies and replica rows never grant authority.
"""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3


VOLUNTARY = frozenset({'continued', 'scheduled', 'completed', 'blocked',
                       'review_requested', 'changes_requested'})


def require_settled(conn, task_id, run_id, outcome):
    """Raise before a voluntary run close; never interfere with crash/reclaim."""
    from hermes_cli.kanban_workspace_publication import _kernel_park
    if outcome not in VOLUNTARY or _kernel_park.get() == task_id:
        return
    from hermes_cli import kanban_db_sync as sync
    if 'uid' not in sync.table_columns(conn, 'task_runs'):
        return  # Local-only boards cannot submit qualified origin forwarding.
    row = conn.execute('SELECT uid FROM task_runs WHERE id=? AND task_id=?', (run_id, task_id)).fetchone()
    if not row or not row[0]:
        return
    from hermes_cli.kanban_capability_registry import hub_db_path
    path = hub_db_path()
    try:
        path.lstat()
    except FileNotFoundError:
        return  # No courier store: there have been no local outbound requests.
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            rows = db.execute("SELECT id,body FROM sent WHERE state NOT IN ('done','rejected') "
                              "AND body LIKE '%origin-forward%'")
            for identity, raw in rows:
                request = json.loads(raw)
                args = request.get('args', {})
                if (request.get('coll') == 'sessions' and request.get('action') == 'origin-forward'
                        and args.get('run_uid') == row[0] and args.get('creator', {}).get('task_id') == task_id):
                    raise ValueError('origin_forward_pending: keep this run alive until request ' + str(identity)
                                     + ' is admitted or definitively rejected. Fetch its courier outcome and retry '
                                     'this lifecycle call; do not recreate or modify the frozen request.')
    except (sqlite3.Error, OSError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('origin_forward_evidence_unavailable: cannot verify outstanding forwards; '
                         'keep this run alive and retry the courier read') from exc
