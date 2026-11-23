"""Native board/catalog inputs; never call these captures from a request. [daveey]

Only trusted startup supplies ``api`` and the board scope. Related live rows
share one transaction; shadow and catalog databases retain independent source
observations. External policy/roster/clock evidence is bounded-refresh, not a
claim of an atomic cross-database snapshot or instantaneous external truth.
"""
from contextlib import closing
import sqlite3
import time

from dataclasses import asdict
from hermes_cli import kanban_task_change as changes
from hermes_cli import kanban_shadow_view as shadow_view



def _observation(conn):
    _dirty, cursor = changes.changed_since(conn, None, limit=1)
    return {'content': asdict(cursor) if cursor is not None else None,
            'schema': conn.execute('PRAGMA schema_version').fetchone()[0]}


def _board(api, board, selector):
    observed_at = time.time()
    with closing(api._conn(board=board)) as live:
        live.execute('BEGIN')
        local = api.board_fact_capture.capture(live, **selector)
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
                 'counts': {b['slug']: api.board_counts._query(kanban_db.kanban_db_path(board=b['slug']))[0] for b in boards},
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
