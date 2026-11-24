"""Native board/catalog inputs; never call these captures from a request. [daveey]

Only trusted startup supplies ``api`` and the board scope. Related live rows
share one transaction; shadow and catalog databases retain independent source
observations. External policy/roster/clock evidence is bounded-refresh, not a
claim of an atomic cross-database snapshot or instantaneous external truth.
"""
from collections import OrderedDict
from contextlib import closing
import os
import sqlite3
import threading
import time

from dataclasses import asdict
from hermes_cli import kanban_task_change as changes
from hermes_cli import kanban_shadow_view as shadow_view



def _observation(conn):
    _dirty, cursor = changes.changed_since(conn, None, limit=1)
    return {'content': asdict(cursor) if cursor is not None else None,
            'schema': conn.execute('PRAGMA schema_version').fetchone()[0]}


# (board file, its identity, selector) -> (change cursor, rows) of the last capture: the next one reads only what
# moved. A replaced file has a new identity, so it starts from a full read, as the legacy route's dropped observer.
_RETAINED: OrderedDict = OrderedDict()
_RETAINED_MAX = 8  # as board_rows.MAX_ENTRIES
_retained_lock = threading.Lock()


def _local(api, live, board, selector):
    """The board's own rows, incrementally from the last capture of the same query (as ``board_rows``)."""
    from hermes_cli import kanban_db
    path = str(kanban_db.kanban_db_path(board=board))
    try:
        stat = os.stat(path)
    except OSError:
        key = None  # no identity to vouch for retained rows: read in full, retain nothing
    else:
        key = (path, stat.st_dev, stat.st_ino, tuple(sorted(selector.items())))
    with _retained_lock:
        hit = _RETAINED.get(key) if key is not None else None
    try:
        changed, cursor = changes.changed_since(live, hit[0] if hit else None)
        local, rows = api.board_fact_capture.capture_incremental(live, hit[1] if hit else None, changed, **selector)
    except BaseException:
        with _retained_lock:
            _RETAINED.pop(key, None)  # a failed read never seeds the next delta
        raise
    if key is None:
        return local
    with _retained_lock:
        _RETAINED[key] = (cursor, rows)
        _RETAINED.move_to_end(key)
        while len(_RETAINED) > _RETAINED_MAX:
            _RETAINED.popitem(last=False)
    return local


def retire(paths) -> None:
    """Drop retained rows of closed board files (``BoardRuntime.close``), as ``board_rows.retire``."""
    paths = {str(path) for path in paths}
    with _retained_lock:
        for key in [key for key in _RETAINED if key[0] in paths]:
            del _RETAINED[key]
        for path in paths:
            _COUNTS.pop(path, None)


# path -> (st_dev, st_ino, (counts, state)): the last board_counts._query result, so the next catalog capture
# reads only what moved. Taken out while refreshed (one owner of the mutable state), put back only on success.
_COUNTS: OrderedDict = OrderedDict()
_COUNTS_MAX = 64


def _counts(api, path):
    """Status counts of the board file, incrementally; errors propagate and leave nothing retained."""
    key = str(path)
    try:
        stat = os.stat(path)
        ident = (stat.st_dev, stat.st_ino)
    except OSError:
        ident = None  # no identity to vouch for retained state: full read, retain nothing
    with _retained_lock:
        hit = _COUNTS.pop(key, None)
        prev = hit[2] if hit is not None and ident is not None and hit[:2] == ident else None
        # _query mutates prev's status map; serialize refresh and retire so no older capture can overwrite a newer one.
        counts, state = api.board_counts._query(path, prev)
        if ident is not None and state is not None:
            _COUNTS[key] = (*ident, (counts, state))
            _COUNTS.move_to_end(key)
            while len(_COUNTS) > _COUNTS_MAX:
                _COUNTS.popitem(last=False)
        return dict(counts)


def _board(api, board, selector):
    observed_at = time.time()
    with closing(api._conn(board=board)) as live:
        live.execute('BEGIN')
        local = _local(api, live, board, selector)
        observations = {'local': _observation(live)}
        shadow = None
        try:
            with shadow_view.open_shadow_replica(live) as replica:
                if replica is not None:
                    replica.execute('BEGIN')
                    observations['replica'] = _observation(replica)
                    shadow = api.board_shadow.capture(live, replica, api.board_fact_capture.project(local),
                        selector['tenant'], selector['include_archived'], selector['workflow_template_id'],
                        selector['current_step_key'], selector['done_limit'])
                    replica.rollback()
        except sqlite3.Error:
            # Canonical shadow failure preserves healthy live cards. Record the
            # degraded source separately rather than claiming a healthy empty replica.
            observations['replica'] = {'health': 'failed'}
        live.rollback()
    return {'value': {'local': local, 'shadow': shadow}, 'observed_at': observed_at,
            'observations': observations}


def capture(api, board, view, selector):
    """Gather detached canonical inputs on the authorized auxiliary worker."""
    if view == 'board':
        return _board(api, board, selector)
    if view not in ('projects', 'boards'):
        raise ValueError('unknown board view')
    from hermes_cli import kanban_db, projects_db
    observed_at = time.time()
    with projects_db.connect_closing() as conn:
        conn.execute('BEGIN')
        rows = [p.to_dict() for p in projects_db.list_projects(conn, include_archived=view == 'boards')]
        schema = conn.execute('PRAGMA schema_version').fetchone()[0]
        conn.rollback()
    if view == 'projects':
        value = {'rows': rows}
    else:
        boards = kanban_db.list_boards(include_archived=selector['include_archived'])
        value = {'rows': boards, 'current': kanban_db.get_current_board(),
                 'counts': {b['slug']: _counts(api, kanban_db.kanban_db_path(board=b['slug'])) for b in boards},
                 'workspace_kinds': {b['slug']: api._default_workspace_kind(b) for b in boards},
                 'project_names': {p['id']: p['name'] for p in rows}}
    return {'value': value, 'observed_at': observed_at, 'observations': {'catalog': {'schema': schema}}}


def project(api, view, captured):
    """Canonical serializers only; all discovery and dynamic evidence are captured."""
    value = api.board_fact_capture._plain(captured)
    if view == 'board':
        local = api.board_fact_capture.project(value['local'])
        return local if value['shadow'] is None else api.board_shadow.project_captured(local, value['shadow'])
    if view == 'projects':
        return api.board_catalog_projection.projects(value['rows'])
    if view == 'boards':
        return api.board_catalog_projection.boards(value['rows'], value['current'], counts=value['counts'],
            workspace_kinds=value['workspace_kinds'], project_names=value['project_names'])
    raise ValueError('unknown board view')
