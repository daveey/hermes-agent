"""Bounded durable feed custody while a source awaits authenticated STOP.

This queue conveys no release authority. Exact prepared-row SQL fences remain
installed; replay re-enters the ordinary native apply/containment path. [daveey]
"""
from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import asdict

MAX_ROWS = 4096
MAX_BYTES = 16 * 1024 * 1024
REPLAY_LIMIT = 16
_TABLE = 'kanban_home_feed1'


def present(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (_TABLE,)).fetchone())


def pending(conn) -> bool:
    return present(conn) and bool(conn.execute(f'SELECT 1 FROM {_TABLE} LIMIT 1').fetchone())


def active(conn) -> bool:
    if pending(conn):
        return True
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_home_prepared1'").fetchone():
        return False
    from hermes_cli.kanban_home_abort import predicate
    from hermes_cli.kanban_home_finalized import predicate as finalized
    return bool(conn.execute('SELECT 1 FROM kanban_home_prepared1 p WHERE NOT EXISTS '
                             f'(SELECT 1 FROM kanban_home_finalized1 f WHERE {finalized()}) '
                             f'AND NOT EXISTS(SELECT 1 FROM kanban_home_aborted1 a WHERE {predicate("p")}) LIMIT 1').fetchone())


def blocked(conn, change) -> bool:
    """Preflight ordinary identities, including deletes and old link endpoints.

    The savepoint/SQL fence below remains the final check for unusual conflicting
    unique identities; no Python identity check can replace that fence.
    """
    from hermes_cli.kanban_home_transfer import frozen
    ids = {change.fields.get('task_id'), change.fields.get('parent_id'), change.fields.get('child_id')}
    if change.table == 'tasks':
        ids.add(change.uid)
    elif change.table in ('task_comments', 'task_events', 'task_runs', 'task_attachments', 'task_chat_links'):
        row = conn.execute(f'SELECT task_id FROM {change.table} WHERE uid=?', (change.uid,)).fetchone()
        if row:
            ids.add(row[0])
    # A post-STOP pending generation blocks local intent, not authenticated feed.
    # Only an unreleased preparation requires deferral of the native feed itself.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_home_prepared1'").fetchone():
        return False
    from hermes_cli.kanban_home_abort import aborted
    from hermes_cli.kanban_home_finalized import predicate as finalized
    return any(conn.execute('SELECT 1 FROM kanban_home_prepared1 p WHERE task_id=? AND NOT EXISTS '
                            f'(SELECT 1 FROM kanban_home_finalized1 f WHERE {finalized()})',
                            (tid,)).fetchone() and not aborted(conn, tid) and frozen(conn, tid)
               for tid in ids if isinstance(tid, str))


def _install(conn):
    # No task_id column: this is transport custody, not a mutable business row.
    conn.execute(f'CREATE TABLE IF NOT EXISTS {_TABLE} (id INTEGER PRIMARY KEY, table_name TEXT NOT NULL, '
                 'row_uid TEXT NOT NULL, wire TEXT NOT NULL UNIQUE, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT)')
    from hermes_cli.sqlite_util import add_column_if_missing
    add_column_if_missing(conn, _TABLE, 'last_error', 'last_error TEXT')
    conn.execute(f'CREATE INDEX IF NOT EXISTS home_feed1_row ON {_TABLE}(table_name,row_uid,id)')


def _store(conn, ch, result):
    _install(conn)
    wire = json.dumps(dict(change=asdict(ch), local_pending_wins=result.local_pending_wins),
                      sort_keys=True, separators=(',', ':'), allow_nan=False)
    if conn.execute(f'SELECT 1 FROM {_TABLE} WHERE wire=?', (wire,)).fetchone():
        return
    count, size = conn.execute(f'SELECT count(*),COALESCE(sum(length(CAST(wire AS BLOB))),0) FROM {_TABLE}').fetchone()
    if count >= MAX_ROWS or size + len(wire.encode('utf-8')) > MAX_BYTES:
        raise ValueError('home deferred feed custody capacity exceeded')
    conn.execute(f'INSERT INTO {_TABLE}(table_name,row_uid,wire) VALUES (?,?,?)', (ch.table, ch.uid, wire))


def _predecessor(conn, ch, rowid):
    return present(conn) and conn.execute(
        f'SELECT 1 FROM {_TABLE} WHERE table_name=? AND row_uid=? AND id<? LIMIT 1',
        (ch.table, ch.uid, rowid if rowid is not None else 9223372036854775807)).fetchone()


def run(conn, ch, own, result, apply, *, rowid=None):
    """Rollback one prepared-row effect, then retain its exact wire before cursor ACK."""
    from hermes_cli import kanban_containment_replica as containment
    if blocked(conn, ch) or _predecessor(conn, ch, rowid):
        if rowid is None:
            _store(conn, ch, result)
        result.deferred += 1
        return False
    before = copy.deepcopy(vars(result))
    conn.execute('SAVEPOINT home_feed_row')
    try:
        containment.prepare_apply(conn, [ch])
        with containment.projection(conn, ch):
            apply(conn, ch, own, result)
    except sqlite3.IntegrityError as exc:
        conn.execute('ROLLBACK TO home_feed_row')
        if str(exc) != 'home transfer prepared':
            raise
        vars(result).clear()
        vars(result).update(before)
        if rowid is None:
            _store(conn, ch, result)
        result.deferred += 1
        return False
    finally:
        conn.execute('RELEASE home_feed_row')
    if rowid is not None:
        conn.execute(f'DELETE FROM {_TABLE} WHERE id=?', (rowid,))
    return True


def replay(conn, own, result, apply, rowid):
    """Replay exactly one retained wire in its own bounded writer reservation."""
    from hermes_cli.kanban_db_sync import RemoteChange
    row = conn.execute(f'SELECT wire FROM {_TABLE} WHERE id=?', (rowid,)).fetchone()
    if not row:
        return []
    value = json.loads(row[0])
    ch = RemoteChange(**value['change'])
    old = result.local_pending_wins
    result.local_pending_wins = value['local_pending_wins']
    try:
        if run(conn, ch, own, result, apply, rowid=rowid):
            return [ch]
        conn.execute(f'UPDATE {_TABLE} SET attempts=attempts+1 WHERE id=?', (rowid,))
        return []
    finally:
        result.local_pending_wins = old


def drain(conn, *, board=None, observers=True):
    """Only the native pull calls this, separately from incoming-page admission.

    Each attempt has the existing 250ms feed VM/Python budget; a poison or slow
    retained row stays durable and cannot abort the independent incoming page.
    Failures retain exact wire bytes and a bounded diagnostic, never authority.
    """
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_sync_apply_budget import apply_page
    if not pending(conn):
        return {}
    rows = conn.execute(f'SELECT id FROM {_TABLE} ORDER BY attempts,id LIMIT ?', (REPLAY_LIMIT,)).fetchall()
    changed = {}
    for (rowid,) in rows:
        try:
            cursor = int((sync.sync_state(conn) or {}).get('cursor') or 0)
            result = apply_page(conn, [], new_cursor=cursor, board=board, observers=observers, home_replay_id=rowid)
            changed.update(result.status_changed)
        except Exception as exc:  # health: allow BLE001 -- retain poison replay without starving the native feed
            # The failed reservation has rolled back before this independent
            # small diagnostic write. Never mutate/drop the captured wire.
            error = f'{type(exc).__name__}: {exc}'[:1000]
            with write_txn(conn):
                _install(conn)
                prior = conn.execute(f'SELECT last_error FROM {_TABLE} WHERE id=?', (rowid,)).fetchone()
                conn.execute(f'UPDATE {_TABLE} SET attempts=attempts+1,last_error=? WHERE id=?', (error, rowid))
            if prior is not None and prior[0] != error:
                import logging
                logging.getLogger(__name__).warning('home feed replay retained row=%s error_type=%s', rowid, type(exc).__name__)
    return changed
