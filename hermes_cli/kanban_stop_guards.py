"""Persistent SQL fences for ``administrative_stop_restricted`` cards. [daveey]

Stored triggers protect every later writer on this file (reopened connections, older local
code, maintenance) without changing sync selection. A stopped card stays ``triage`` with no
run, claim or worker; it gets no new runs; its runs, evidence and dependency edges (either
direction) are immutable; it cannot be deleted, archived, completed or REPLACEd. Unrelated
cards are untouched: every predicate starts from the stopped card's own evidence row.
The one allowance is the supported NULL -> uid bootstrap with every other column equal.
"""
from __future__ import annotations

from hermes_cli.kanban_stop_ended import KIND

VERSION = 1
MARKER = 'kanban_stop_ended_guard'
_STOPPED = f"SELECT task_id FROM task_events WHERE kind='{KIND}'"


def _uid_only(table: str, columns: set[str]) -> str:
    # The NULL -> uid bootstrap, or an exact no-op rewrite (a sync echo of this gateway's own row).
    same = ' AND '.join(f'NEW.{c} IS OLD.{c}' for c in sorted(columns - {'uid'}))
    return f'(({same}) AND (NEW.uid IS OLD.uid OR (OLD.uid IS NULL AND NEW.uid IS NOT NULL)))'


def _columns(conn, table):
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}


def ddl(conn) -> dict[str, tuple[str, str]]:
    task_cols, run_cols, event_cols = _columns(conn, 'tasks'), _columns(conn, 'task_runs'), _columns(conn, 'task_events')
    pinned = ["NEW.status IS NOT 'triage'", 'NEW.current_run_id IS NOT NULL', 'NEW.claim_lock IS NOT NULL',
              'NEW.claim_expires IS NOT NULL']
    pinned += [f'NEW.{c} IS NOT NULL' for c in ('worker_pid', 'worker_started_at') if c in task_cols]
    pinned += [f'NEW.{c} IS NOT OLD.{c}' for c in ('title', 'body', 'assignee', 'created_on', 'tenant') if c in task_cols]
    runs_uid = _uid_only('task_runs', run_cols) if 'uid' in run_cols else '0'
    events_uid = _uid_only('task_events', event_cols) if 'uid' in event_cols else '0'
    links = f'OLD.parent_id IN ({_STOPPED}) OR OLD.child_id IN ({_STOPPED})'
    new_links = f'NEW.parent_id IN ({_STOPPED}) OR NEW.child_id IN ({_STOPPED})'
    evidence_replace = ('EXISTS (SELECT 1 FROM task_events o WHERE o.id=NEW.id AND o.task_id IN '
                        f'({_STOPPED}))')
    if 'uid' in event_cols:
        evidence_replace = ('EXISTS (SELECT 1 FROM task_events o WHERE (o.id=NEW.id OR (NEW.uid IS NOT NULL AND o.uid=NEW.uid)) '
                            f'AND o.task_id IN ({_STOPPED}))')
    return {
        'task_update': ('BEFORE UPDATE ON tasks', f"OLD.id IN ({_STOPPED}) AND ({' OR '.join(pinned)})"),
        'task_delete': ('BEFORE DELETE ON tasks', f'OLD.id IN ({_STOPPED})'),
        # INSERT OR REPLACE fires INSERT (not DELETE) triggers with recursive_triggers OFF.
        'task_replace': ('BEFORE INSERT ON tasks', f'NEW.id IN ({_STOPPED})'),
        'run_insert': ('BEFORE INSERT ON task_runs', f'NEW.task_id IN ({_STOPPED})'),
        'run_update': ('BEFORE UPDATE ON task_runs', f'OLD.task_id IN ({_STOPPED}) AND NOT {runs_uid}'),
        'run_delete': ('BEFORE DELETE ON task_runs', f'OLD.task_id IN ({_STOPPED})'),
        'event_update': ('BEFORE UPDATE ON task_events', f'OLD.task_id IN ({_STOPPED}) AND NOT {events_uid}'),
        'event_delete': ('BEFORE DELETE ON task_events', f'OLD.task_id IN ({_STOPPED})'),
        'event_replace': ('BEFORE INSERT ON task_events', evidence_replace),
        'link_insert': ('BEFORE INSERT ON task_links', new_links),
        'link_update': ('BEFORE UPDATE ON task_links', f'{links} OR {new_links}'),
        'link_delete': ('BEFORE DELETE ON task_links', links),
    }


def install(conn) -> None:
    """Idempotent; runs inside the caller's write transaction, so no connection sees an unfenced gap."""
    conn.execute(f'CREATE TABLE IF NOT EXISTS {MARKER} (version INTEGER NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_stop_ended_kind ON task_events(kind, task_id)')
    for name, (when, predicate) in ddl(conn).items():
        trigger = f'stop_ended_{name}'
        sql = f"CREATE TRIGGER {trigger} {when} WHEN {predicate} BEGIN SELECT RAISE(ABORT,'administrative_stop_immutable'); END"
        old = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)).fetchone()
        if old and old[0] == sql:
            continue
        conn.execute(f'DROP TRIGGER IF EXISTS {trigger}')
        conn.execute(sql)
    if not conn.execute(f'SELECT 1 FROM {MARKER} WHERE version>=?', (VERSION,)).fetchone():
        conn.execute(f'INSERT INTO {MARKER} (version) VALUES (?)', (VERSION,))


def installed(conn) -> bool:
    """Fail closed: a stopped card with a missing or partial fence set is reported unguarded."""
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'stop_ended_%'")}
    return names >= {f'stop_ended_{n}' for n in ddl(conn)}
