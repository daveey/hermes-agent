"""Reuse detached local selection, never time/authority-dependent enrichment."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
import sqlite3
from threading import RLock

from hermes_cli.kanban_annotation_watch import signature
from hermes_cli.kanban_execution_source import transaction
from hermes_cli.kanban_session_outcome_tasks import select_listed_local_task_set, select_listed_local_tasks
from hermes_cli.kanban_table_columns import table_columns

_LOCK = RLock()
_MEMO = OrderedDict()
_MAX_ENTRIES = 32
_MAX_CHARS = 131072


def _token(conn, path):
    return signature((), [str(path)], [str(path) + '-shm']), conn.total_changes


@contextmanager
def selected(conn, path, ids, statuses):
    """Yield owned rows and a fresh observation clock in the caller's snapshot.

    One snapshot-anchor read is intentional even on a hit: enrichment must see
    the same snapshot as the cached selection. Capture BEFORE BEGIN and fence
    after anchoring; sampling only after BEGIN can certify an older snapshot.
    Optional legacy selection failures are returned but never memoized.
    """
    owned = not conn.in_transaction
    before = _token(conn, path) if owned else None
    key = (conn, str(path), tuple(ids), tuple(statuses))
    with transaction(conn) as at:
        conn.row_factory = sqlite3.Row
        conn.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
        stable = owned and before == _token(conn, path)
        with _LOCK:
            entry = _MEMO.get(key) if stable else None
            if entry is not None and entry[0] == before:
                _MEMO.move_to_end(key)
                rows = deepcopy(entry[1])
            else:
                entry = None
        if entry is None:
            rows = None
            complete = True
            if 'session_id' in table_columns(conn, 'tasks'):
                try:
                    rows = select_listed_local_task_set(conn, ids, statuses)[0]
                except sqlite3.Error:
                    complete = False
                    rows = select_listed_local_tasks(conn, ids, statuses)
            if stable and complete and before == _token(conn, path):
                # Bound retained payload as well as the number of readers/scopes.
                if len(repr(rows)) <= _MAX_CHARS:
                    with _LOCK:
                        _MEMO[key] = (before, deepcopy(rows))
                        _MEMO.move_to_end(key)
                        while len(_MEMO) > _MAX_ENTRIES:
                            _MEMO.popitem(last=False)
        yield rows, at
