"""Untrusted worker route discovery; never grants origin authority.

The origin-local forward receiver checks its accepted obligation and a fresh
Board lease. Replica data here is only enough to address that receiver.
"""
from __future__ import annotations

import os
import re
import sqlite3

from hermes_cli.kanban_selfwork import SelfWorkError

_ID = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}')


def worker_creator():
    from hermes_cli import kanban_db as kb, kanban_origin
    from hermes_cli.kanban_identity import _connect
    from hermes_cli.kanban_placement_forward import opened_board

    task = os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_KANBAN_WORKER_TASK')
    markers = {os.environ[k] for k in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK') if os.environ.get(k)}
    run = os.environ.get('HERMES_KANBAN_RUN_ID', '')
    if (len(markers) > 1 or not task or not _ID.fullmatch(task)
            or not re.fullmatch(r'[0-9]{1,19}', run) or not 0 < int(run) <= 9223372036854775807):
        raise SelfWorkError('self-work worker requires authorized origin-gateway forwarding: missing creator run')
    try:
        conn = _connect(kb.kanban_db_path())
        try:
            board = opened_board(conn)
            row = conn.execute('''SELECT t.created_on,r.uid FROM tasks t JOIN task_runs r
                ON r.task_id=t.id AND r.id=t.current_run_id
                WHERE t.id=? AND r.id=? AND t.status='running' AND r.status='running'
                AND r.ended_at IS NULL''', (task, int(run))).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        raise SelfWorkError('self-work creator discovery unavailable; never create locally') from exc
    if row is None:
        raise SelfWorkError('self-work creator run is not current')
    gateway, uid = row
    owners = {(gw, profile) for gw, profile in kanban_origin._replica_task_owners(task, board) if gw == gateway}
    if len(owners) != 1:
        raise SelfWorkError('self-work creator publication namespace is missing or ambiguous')
    creator = dict(gateway=gateway, profile=next(iter(owners))[1], board=board, task_id=task, run_uid=uid)
    if not all(isinstance(v, str) and _ID.fullmatch(v) for v in creator.values()):
        raise SelfWorkError('self-work creator identity is invalid')
    return creator
