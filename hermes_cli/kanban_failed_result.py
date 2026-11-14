"""Conservative, board-only proof for failed-result reconciliation. [daveey]

No owner session store access and no signals: an unavailable identity is not
proof of death. The caller must hold the board write transaction when using a
proof to close a run. This module does not itself change lifecycle state.
"""
from __future__ import annotations

import json
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
    import psutil

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if not valid_birth(birth):
        return False
    try:
        if not psutil.pid_exists(pid):
            return True
    except (psutil.Error, OSError):
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


def expiry_identity_matches(conn, selected) -> bool:
    """An expiry decision belongs only to the selected run, claim and outer incarnation."""
    row = conn.execute('SELECT status, current_run_id, claim_lock, worker_pid, worker_started_at '
                       'FROM tasks WHERE id=?', (selected['id'],)).fetchone()
    return row is not None and row['status'] == 'running' and all(
        row[key] == selected[key]
        for key in ('current_run_id', 'claim_lock', 'worker_pid', 'worker_started_at'))


def termination_targets(conn, task_id, *, selected=None):
    """Plan expiry termination for a modern-result run, or ``None`` to hold.

    Witnesses are the outer worker plus EVERY historical ``worker_bound`` incarnation of the
    run. All must carry a valid birth and be either exited or the exact recorded process; one
    recycled, unreadable or unverified witness holds the whole plan so nothing is signalled.
    Returns the live exact-identity ``(pid, birth, is_outer)`` targets (may be empty).
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as dispatch
    row = selected if selected is not None else conn.execute(
        'SELECT current_run_id, claim_lock, worker_pid, worker_started_at FROM tasks WHERE id=?',
        (task_id,)).fetchone()
    if (row is None or (selected is not None and not expiry_identity_matches(conn, selected))
            or not kb._claim_is_local(row['claim_lock'] or '', kb._host_prefix())):
        return None
    witnesses = [(row['worker_pid'], row['worker_started_at'], True)]
    for event in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                              "AND kind='worker_bound'", (task_id, row['current_run_id'])):
        try:
            bound = json.loads(event['payload'])
        except (TypeError, ValueError):
            return None
        if not isinstance(bound, dict) or bound.get('claim_lock') != row['claim_lock']:
            return None
        witnesses.append((bound.get('pid'), bound.get('started_at'), False))
    targets = {}
    for pid, birth, outer in witnesses:
        if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not valid_birth(birth)):
            return None
        if process_gone(pid, birth):
            continue
        if dispatch._process_fingerprint(pid) != birth:
            return None
        targets[(pid, birth)] = targets.get((pid, birth), False) or outer
    return [(pid, birth, outer) for (pid, birth), outer in targets.items()]


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


def failed_result_receipt(conn, task_id, run_id, claim_lock, *, allow_continuable=False, allow_clean=False):
    """Return an attributable failure independently of its process death proof.

    A receipt with an unproven death must veto legacy reclaim, not fall through
    to its weaker outer-process-only checks. ``allow_clean`` (the dispatcher only) also
    accepts a modern exit 0: a run whose evidence is modern has no legacy path left, so
    its clean result must be attributable here or the run is never closed. [daveey]
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
    from hermes_cli.kanban_failed_result_identity import attributable_events
    events = attributable_events(conn, task_id, run_id)
    if events is None:
        return None
    (bound_event, bound), (result_event, result), modern = events
    owner = modern and result is not None and result.get('result_role') == 'owner_restart'
    if (bound is None or result is None or (not owner and bound.get('claim_lock') != claim_lock)
            or result.get('claim_lock') != claim_lock):
        return None
    if owner and (bound.get('pid'), bound.get('started_at')) != (row['worker_pid'], row['worker_started_at']):
        return None
    code = result.get('exit_code')
    loss = result_event['kind'] == 'worker_lost' and result.get('loss_receipt') is True
    if loss and (result.get('bound') is not True or code is not None):
        return None
    if ((not loss and (not isinstance(code, int) or isinstance(code, bool))) or (code == 0 and not (allow_clean and modern))
            or (result.get('continuable') and not (owner and allow_continuable))
            or (not modern and result_event['id'] <= bound_event['id'])
            or result.get('pid') != bound.get('pid')):
        return None
    portable = {'source_result_event_uid': result_event['uid']} if 'uid' in result_event.keys() else {}
    return {'source_result_event_id': result_event['id'], **portable,
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
    proof = failed_result_proof(conn, *identity, allow_continuable=not failed_results_only,
                                allow_clean=not failed_results_only)
    hold = proof is None and (failed_results_only or modern_result_recorded(conn, *identity[:2]))
    return proof, hold


def result_exit_code(conn, row, proof):
    """Use the causally selected result; legacy launches retain legacy lookup."""
    if proof is not None:
        return proof['result']['exit_code']
    from hermes_cli.kanban_owner_recovery import managed_run_exit_code
    return managed_run_exit_code(conn, row['id'], row['worker_pid'], row['claim_lock'])


def failed_result_proof(conn, task_id, run_id, claim_lock, *, allow_continuable=False, allow_clean=False):
    """Return an attributable failure after outer and every bound incarnation exit.

    Only the dispatcher opts into continuable bookings, after their submitter
    is gone. Observers must leave checkpoints available to a replacement owner.
    """
    receipt = failed_result_receipt(conn, task_id, run_id, claim_lock,
                                    allow_continuable=allow_continuable, allow_clean=allow_clean)
    if receipt is None:
        return None
    if not process_gone(*receipt.pop('outer')) or not process_gone(*receipt.pop('bound')):
        return None
    # Continued attempts can have historical bindings. Superseding their result
    # does not remove their process-liveness fence.
    for event in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                              "AND kind='worker_bound'", (task_id, run_id)):
        try:
            bound = json.loads(event['payload'])
        except (TypeError, ValueError):
            return None
        if (not isinstance(bound, dict) or bound.get('claim_lock') != claim_lock
                or not process_gone(bound.get('pid'), bound.get('started_at'))):
            return None
    return receipt
