"""Host-local, admission-bound model evidence carried by portable run events."""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

from hermes_constants import get_hermes_home
from hermes_cli.sqlite_safe_read import connect_tracked

TERMINAL_KINDS = frozenset({'completed', 'blocked', 'continued', 'review_requested', 'scheduled',
                            'dependency_wait', 'worker_result', 'crashed', 'timed_out', 'reclaimed',
                            'stale', 'reconciled', 'protocol_violation', 'rate_limited', 'owner_restart_lost',
                            'owner_unavailable', 'host_reboot', 'oom_killed', 'signaled'})


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _concrete(value):
    return isinstance(value, str) and bool(value.strip()) and not value.strip().casefold().startswith('class:')


def local_gateway(conn):
    row = conn.execute('SELECT gateway_id FROM kanban_sync_state LIMIT 1').fetchone()
    return row[0] if row and isinstance(row[0], str) and row[0] else None


def evidence(conn, task_id, run_id):
    """Prove every admission against this host's managed store, opened read-only.

    Imported integer run ids are intentionally not compared with request ids.
    Local event origins are NULL until capture; replayed origins must match us.
    Unknown or unreadable evidence grants nothing and never blocks a lifecycle.
    Each admitted root includes all transitive parent_session_id descendants,
    without applying the root's admission-time window to later child sessions.
    A session with no concrete model in either sessions or usage invalidates the
    whole receipt; selectors/blanks may be resolved by concrete usage as usual.
    Missing admitted/reached session rows fail closed. Orphan usage whose session
    row is missing has no recoverable parent linkage: it cannot be assigned to a
    run (nor can children behind a deleted intermediate row). No lineage is invented.
    """
    if run_id is None:
        return None
    try:
        gateway = local_gateway(conn)
        if not gateway:
            return None
        run = conn.execute('SELECT uid, started_at, ended_at FROM task_runs WHERE task_id=? AND id=?',
                           (task_id, run_id)).fetchone()
        if not run or not run[0] or not _finite(run[1]):
            return None
        admissions = conn.execute("SELECT payload, created_at, origin_gateway FROM task_events "
                                  "WHERE task_id=? AND run_id=? AND kind='owner_admitted'",
                                  (task_id, run_id)).fetchall()
        if not admissions:
            return None
        expected = (get_hermes_home() / 'state.db').resolve()
        sessions, models = set(), set()
        uri = expected.as_uri() + '?mode=ro'
        with closing(connect_tracked(uri, uri=True, timeout=2, wait_for_tracking=False)) as db:
            db.execute('BEGIN')  # One coherent read-only lineage/model snapshot.
            for raw, admitted, origin in admissions:
                payload = json.loads(raw or '{}')
                if not isinstance(payload, dict) or (origin is not None and origin != gateway):
                    return None
                path, session = payload.get('db'), payload.get('session_id')
                if (not isinstance(path, str) or not Path(path).is_absolute()
                        or Path(path).resolve() != expected or not isinstance(session, str) or not session
                        or not _finite(admitted) or admitted < run[1]):
                    return None
                row = db.execute('SELECT model, started_at FROM sessions WHERE id=?', (session,)).fetchone()
                if (not row or not _finite(row[1]) or not run[1] <= row[1] < admitted + 1
                        or (run[2] is not None and (not _finite(run[2]) or row[1] >= run[2] + 1))):
                    return None
                pending, visited = [session], set()
                while pending:
                    current = pending.pop()
                    if current in visited:
                        return None  # A parent cycle cannot establish a trustworthy tree.
                    visited.add(current)
                    current_row = db.execute('SELECT model FROM sessions WHERE id=?', (current,)).fetchone()
                    if not current_row:
                        return None
                    usage = db.execute('SELECT model FROM session_model_usage WHERE session_id=?', (current,))
                    names = {m.strip() for m in [current_row[0], *(r[0] for r in usage)] if _concrete(m)}
                    if not names:
                        return None
                    models.update(names)
                    sessions.add(current)
                    pending.extend(r[0] for r in db.execute(
                        'SELECT id FROM sessions WHERE parent_session_id=?', (current,)))
        return {'run_uid': run[0], 'session_ids': sorted(sessions), 'session_models': sorted(models),
                'source': 'managed_state_db'}
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None


def stamp(conn, task_id, kind, payload, run_id):
    if kind not in TERMINAL_KINDS:
        return payload
    result = dict(payload or {})
    result.pop('session_models', None)  # Never accept caller-supplied model testimony.
    receipt = evidence(conn, task_id, run_id)
    if receipt:
        result['session_models'] = receipt['session_models']
    return result
