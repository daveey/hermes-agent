"""Read-only producer-wait evidence from the caller's selected board snapshot. [daveey]"""
import json
import math
import sqlite3
import time

from pathlib import Path

from hermes_cli.kanban_table_columns import table_columns


def _selected_board(conn):
    """Name only recognized selected-file layouts; never discover ambient boards."""
    from hermes_cli.kanban_db_sync import board_slug_for_path
    path = next((r[2] for r in conn.execute('PRAGMA database_list') if r[1] == 'main'), '')
    if not path:
        return None
    file = Path(path)
    if file.name == 'kanban.db' or file.parent.name in ('sync-shadow', 'sync-links'):
        return board_slug_for_path(path)
    return None


def overlay(conn, info, parked, now=None):
    """An active named card producer is a wait, never this owner's running executor.

    The clock starts at the current park event, not at the last message/update.
    A missing/terminal/unknown producer or mismatched owner supplies no evidence.
    No process-home replica discovery or network reads are permitted here.
    """
    columns = table_columns(conn, 'tasks')
    now = time.time() if now is None else now
    owner = 'created_on' if 'created_on' in columns else 'NULL'
    candidates = []
    for tid, wait in info.items():
        awaiting = parked.get(tid, (None,) * 4)[3]
        if isinstance(awaiting, str):
            try:
                awaiting = json.loads(awaiting)
            except ValueError:
                continue
        since = wait.get('waiting_since')
        if (not isinstance(awaiting, dict) or awaiting.get('kind') != 'card'
                or not isinstance(awaiting.get('ref'), str) or wait.get('wait_state') != 'waiting'
                or isinstance(since, bool) or not isinstance(since, (int, float))
                or not math.isfinite(since) or since <= 0 or since > now):
            continue
        candidates.append((tid, awaiting, since))
    if not candidates:
        return
    refs = json.dumps(list({a['ref'] for _, a, _ in candidates}))
    producers = {r[0]: (r[1], r[2]) for r in conn.execute(
        f'SELECT id, status, {owner} FROM tasks WHERE id IN (SELECT value FROM json_each(?))', (refs,))}
    board = _selected_board(conn)
    for tid, awaited, since in candidates:
        if awaited.get('board') is not None and awaited['board'] != board:
            continue
        state, gateway = producers.get(awaited['ref'], (None, None))
        if state not in ('running', 'review') or (awaited.get('gateway') and awaited['gateway'] != gateway):
            continue
        info[tid]['producer_wait'] = {'id': awaited['ref'], 'since': since}


def stamp(conn, rows):
    """Carry the same evidence on timer/count replica rows, before caller freshness gating."""
    from hermes_cli.kanban_session_wait_rows import _describe_waits, _local_wait_rows
    try:
        parked, events = _local_wait_rows(conn, [r['id'] for r in rows], replica=None)
        info = _describe_waits(parked, events)
        overlay(conn, info, parked)
        for row in rows:
            row['timer_producer_wait'] = info.get(row['id'], {}).get('producer_wait')
            wait = info.get(row['id'], {})
            if wait.get('wait_kind') in ('ci', 'clock', 'deploy'):
                row['timer_external_wait'] = wait
    except (sqlite3.Error, ValueError, TypeError):
        return
