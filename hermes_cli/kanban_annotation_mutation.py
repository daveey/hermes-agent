"""Cross-process, per-scope annotation mutation cues. [daveey]

The publisher includes the token captured BEFORE its forced read in the same
atomic header as that read. There is no separate acknowledgement write: failed
publication and a newer cue racing a read therefore remain pending. Tokens are
opaque (not timestamps), so a clock rollback cannot hide a mutation.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sqlite3
import uuid

from hermes_cli import kanban_annotation_disk as disk

log = logging.getLogger(__name__)
_foreground: contextvars.ContextVar[tuple[str, ...] | None] = contextvars.ContextVar(
    'annotation_foreground', default=None)
_TABLE = 'kanban_annotation_own_write'


@contextlib.contextmanager
def foreground(scope: tuple[str, ...] | None = None):
    """Explicit user/tool request custody, not background sync or maintenance."""
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_session_activity as ksa

    token = _foreground.set((scope or ksa._origin_scope()) if host.enabled() else None)
    try:
        yield
    finally:
        _foreground.reset(token)


def arm(conn: sqlite3.Connection) -> tuple[str, ...] | None:
    """Inside outer BEGIN only. TEMP marks roll back with their originating SQL."""
    scope = _foreground.get()
    if scope is None:
        return None
    try:
        conn.execute(f'CREATE TEMP TABLE IF NOT EXISTS {_TABLE} (id INTEGER PRIMARY KEY)')
        conn.execute(f'DELETE FROM temp.{_TABLE}')
        for table in ('tasks', 'task_links', 'task_chat_links'):
            if not conn.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone():
                continue
            for event in ('INSERT', 'DELETE', 'UPDATE'):
                condition = ' WHEN OLD.status IS NOT NEW.status' if table == 'tasks' and event == 'UPDATE' else ''
                columns = ' OF status' if table == 'tasks' and event == 'UPDATE' else ''
                conn.execute(f'CREATE TEMP TRIGGER IF NOT EXISTS annotation_own_{table}_{event} '
                             f'AFTER {event}{columns} ON main.{table}{condition} BEGIN '
                             f'INSERT OR IGNORE INTO {_TABLE} VALUES(1); END')
        return scope
    except sqlite3.Error:
        log.warning('annotation own-write recorder unavailable', exc_info=True)
        return None


def flush(conn: sqlite3.Connection, scope: tuple[str, ...] | None) -> None:
    """Only after successful outer COMMIT, before any other postcommit writer."""
    if scope is None:
        return
    try:
        changed = conn.execute(f'SELECT 1 FROM temp.{_TABLE} LIMIT 1').fetchone()
        if changed:
            from hermes_cli import kanban_annotation_host as host
            host.kick(scope)
    except (sqlite3.Error, OSError, ValueError):
        log.warning('annotation own-write cue unavailable', exc_info=True)



def tool_context(name: str):
    selected = name in {'kanban_complete', 'kanban_link', 'kanban_unlink', 'kanban_create',
                       'kanban_archive', 'kanban_request_review', 'kanban_request_changes',
                       'kanban_block', 'kanban_unblock', 'kanban_continue', 'kanban_schedule'}
    return foreground() if selected else contextlib.nullcontext()


def request_context(plugin: str, method: str, path: str):
    """Only interactive card/link routes, never dispatch/maintenance or GETs."""
    path = path.removeprefix('/api/plugins/kanban')
    selected = (plugin == 'kanban' and method.upper() in {'POST', 'PATCH', 'DELETE'}
                and (path == '/links' or path == '/tasks' or path.startswith('/tasks/')))
    return foreground() if selected else contextlib.nullcontext()


def record(scope: tuple[str, ...]) -> None:
    disk._write_json(disk._path(scope).with_suffix('.mutation.json'),
                     {'scope': list(scope), 'token': uuid.uuid4().hex})


def current(scope: tuple[str, ...]) -> str | None:
    try:
        value = json.loads(disk._path(scope).with_suffix('.mutation.json').read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return None
    if (not isinstance(value, dict) or value.get('scope') != list(scope)
            or not isinstance(value.get('token'), str) or not value['token']):
        raise ValueError('invalid annotation mutation cue')
    return value['token']


def pending(scope: tuple[str, ...]) -> bool:
    token = current(scope)
    if token is None:
        return False
    try:
        header = json.loads(disk._path(scope).with_suffix('.host.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return True
    return (not isinstance(header, dict) or header.get('version') != disk._HOST_VERSION
            or header.get('scope') != list(scope) or header.get('mutation_token') != token)
