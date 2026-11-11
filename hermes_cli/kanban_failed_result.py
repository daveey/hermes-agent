"""Conservative, board-only proof for failed-result reconciliation. [daveey]

No owner session store access and no signals: an unavailable identity is not
proof of death. The caller must hold the board write transaction when using a
proof to close a run. This module does not itself change lifecycle state.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def valid_birth(birth) -> bool:
    """Accept only a nonempty epoch and positive canonical start components."""
    return isinstance(birth, str) and re.fullmatch(
        r'[^|\s]+\|[1-9][0-9]*(?:@[1-9][0-9]*)?', birth) is not None


def process_gone(pid, birth) -> bool:
    """Prove the recorded incarnation gone; fail closed on live/recycled PIDs.

    A birth witness is required even for an absent PID, rejecting legacy and
    unverified bindings. An occupied live or recycled PID is never acted on.
    An exact-identity Linux zombie is exited; permission failures stay unknown.
    """
    from hermes_cli import kanban_db_dispatch as dispatch

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if not valid_birth(birth):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    # A matching Linux zombie has exited even if its wedged parent has not
    # reaped it. Read identity on both sides of the state observation; an
    # occupied recycled PID and unreadable identity always remain vetoes.
    if sys.platform == 'linux' and dispatch._process_fingerprint(pid) == birth:
        try:
            stat = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8')
            fields = stat.rsplit(')', 1)[1].split()
            if fields[0] == 'Z' and dispatch._process_fingerprint(pid) == birth:
                return True
        except (OSError, IndexError):
            return False
    return False


def recorded_process_witnesses(conn, context):
    """Whether this claim was launched with the modern two-process identity contract.

    Legacy imports and unverified launches cannot opt into bounded reconciliation;
    their pre-existing dispatcher policy is intentionally unchanged.
    """
    row = conn.execute('SELECT worker_started_at FROM tasks WHERE id=? AND current_run_id=? AND claim_lock=?',
                       (context['task_id'], context['run_id'], context['claim_lock'])).fetchone()
    bound = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                         "AND kind='worker_bound' ORDER BY id DESC LIMIT 1",
                         (context['task_id'], context['run_id'])).fetchone()
    if row is None or bound is None:
        return False
    try:
        birth = json.loads(bound['payload']).get('started_at')
    except (TypeError, ValueError, AttributeError):
        return False
    return all(valid_birth(value) for value in (row['worker_started_at'], birth))


def failed_result_receipt(conn, task_id, run_id, claim_lock):
    """Return an attributable failure independently of its process death proof.

    A receipt with an unproven death must veto legacy reclaim, not fall through
    to its weaker outer-process-only checks.
    """
    from hermes_cli import kanban_db as kb

    row = conn.execute(
        "SELECT status, current_run_id, claim_lock, worker_pid, worker_started_at "
        "FROM tasks WHERE id=?", (task_id,),
    ).fetchone()
    if (row is None or row['status'] != 'running' or row['current_run_id'] != run_id
            or row['claim_lock'] != claim_lock
            or not kb._claim_is_local(claim_lock or '', kb._host_prefix())):
        return None
    run = conn.execute('SELECT status, ended_at FROM task_runs WHERE id=? AND task_id=?',
                       (run_id, task_id)).fetchone()
    if run is None or run['status'] != 'running' or run['ended_at'] is not None:
        return None
    events = {}
    for kind in ('worker_bound', 'worker_result'):
        event = conn.execute(
            'SELECT id, created_at, payload FROM task_events '
            'WHERE task_id=? AND run_id=? AND kind=? ORDER BY id DESC LIMIT 1',
            (task_id, run_id, kind),
        ).fetchone()
        if event is None:
            return None
        try:
            payload = json.loads(event['payload'])
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get('claim_lock') != claim_lock:
            return None
        events[kind] = (event, payload)
    bound_event, bound = events['worker_bound']
    result_event, result = events['worker_result']
    code = result.get('exit_code')
    if (not isinstance(code, int) or isinstance(code, bool) or code == 0
            or result.get('continuable') or result_event['id'] <= bound_event['id']
            or result.get('pid') != bound.get('pid')):
        return None
    return {'source_result_event_id': result_event['id'],
            'result_recorded_at': result_event['created_at'], 'result': result,
            'outer': (row['worker_pid'], row['worker_started_at']),
            'bound': (bound.get('pid'), bound.get('started_at'))}


def modern_result_recorded(conn, task_id, run_id):
    """A modern run cannot downgrade to legacy recovery when proof is damaged.

    This is a veto, never authority to close. Do not require a valid bound event:
    precisely that missing or malformed evidence must keep recovery fail-closed.
    Keep the marker across later result events (including malformed imports).
    """
    events = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                          "AND kind='worker_result'", (task_id, run_id))
    for event in events:
        try:
            payload = json.loads(event['payload'])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get('death_proof_version') == 1:
            return True
    return False


def reclaim_proof(conn, row, *, failed_results_only=False):
    """Keep strict receipt vetoes distinct from unsupported legacy recovery."""
    identity = (row['id'], row['current_run_id'], row['claim_lock'])
    proof = failed_result_proof(conn, *identity)
    hold = proof is None and (failed_results_only or modern_result_recorded(conn, *identity[:2]))
    return proof, hold


def failed_result_proof(conn, task_id, run_id, claim_lock):
    """Return latest attributable failed result only after both processes exit."""
    receipt = failed_result_receipt(conn, task_id, run_id, claim_lock)
    if receipt is None:
        return None
    if not process_gone(*receipt.pop('outer')) or not process_gone(*receipt.pop('bound')):
        return None
    return receipt
