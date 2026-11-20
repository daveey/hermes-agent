"""Reconstruct source hints for the actual managed exec, not its dispatcher submitter.

Only the local, current run's attached journal supplies these non-secret hints. Scratch cwd and
artifact custody remain unchanged. Nothing is copied from RPC params or a gateway's ambient source.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from hermes_state_runtime import RuntimeStoreError

SOURCE_KEYS = ('HERMES_KANBAN_SOURCE_PATH', 'HERMES_KANBAN_SOURCE_COMMIT', 'HERMES_KANBAN_SOURCE_BRANCH')


def source_worker_env(policy, env):
    """Overlay validated attached source on a private env before Popen (including its initial environ)."""
    if not policy or not policy.kanban_json:
        return env
    context = json.loads(policy.kanban_json)
    base = os.environ if env is None else env
    child = dict(base)
    for key in SOURCE_KEYS:
        child.pop(key, None)
    from hermes_cli.kanban_source_board import attached_run
    try:
        with closing(sqlite3.connect(Path(context['db']).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            source = attached_run(conn, context['run_id'])
            if source is None or source.state != 'attached':
                return child if any(key in base for key in SOURCE_KEYS) else env
            task = conn.execute('SELECT status,current_run_id,claim_lock FROM tasks WHERE id=?',
                                (context['task_id'],)).fetchone()
            run = conn.execute('SELECT task_id,status,claim_lock,ended_at FROM task_runs WHERE id=?',
                               (context['run_id'],)).fetchone()
    except (OSError, sqlite3.Error, ValueError, KeyError) as exc:
        raise RuntimeStoreError('invalid_kanban_source_claim') from exc
    if (source.task_id != context['task_id']
            or task != ('running', context['run_id'], context['claim_lock'])
            or run != (context['task_id'], 'running', context['claim_lock'], None)):
        raise RuntimeStoreError('stale_kanban_source_claim')
    child.update({key: value for key, value in zip(SOURCE_KEYS, (source.path, source.pre_head, source.branch)) if value})
    return child
