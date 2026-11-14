"""Owner-witnessed loss receipts for modern runs, without inventing an exit code."""
from __future__ import annotations

import json


def annotate_loss(dead, proof, now, observer):
    """Keep the authoritative loss reason without consulting the owner's session store."""
    dead.event_payload.update({k: v for k, v in proof.items() if k != 'result'})
    dead.event_payload['reconciled_at'] = now
    dead.event_payload['reconciled_by'] = 'observer' if observer else 'dispatcher'
    result = proof['result']
    if result.get('loss_receipt') is True:
        note = f"managed worker lost: {str(result.get('reason') or 'unknown')[:80]}"
        dead.error_text = f"{note}; {dead.error_text}"
        dead.event_payload['attempt_note'] = note


def loss_identity_fields(conn, context, pid, bound):
    """Called under the owner's loss transaction; a stale interpreter cannot attest a successor."""
    from hermes_cli.kanban_failed_result import modern_result_recorded, recorded_process_witnesses
    from hermes_cli.kanban_failed_result_identity import result_identity_fields
    if bound is not True or type(pid) is not int or pid <= 0:
        return {}
    if not modern_result_recorded(conn, context['task_id'], context['run_id']):
        return {}  # Preserve the legacy loss policy.
    if not recorded_process_witnesses(conn, context):
        return {}
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                         "AND kind='worker_bound' ORDER BY id DESC LIMIT 1",
                         (context['task_id'], context['run_id'])).fetchone()
    payload = json.loads(event['payload'])
    if (payload.get('pid'), payload.get('claim_lock')) != (pid, context['claim_lock']):
        return {}
    from hermes_cli.kanban_failed_result_identity import modern_receipt
    _, selected = modern_receipt(conn, context['task_id'], context['run_id'], context['claim_lock'])
    if selected is not None:
        result = selected[1]
        if (type(result.get('exit_code')) is int
                and result['exit_code'] != 0 and not result.get('continuable')
                and not result.get('result_role')):
            # The interpreter already explained this failed execution. The owner's
            # EOF/unknown echo is diagnostic, not a new result that erases its quota.
            return {}
    return {'death_proof_version': 1, 'loss_receipt': True, 'exit_code': None,
            **result_identity_fields(conn, context)}


def evidence_rows(conn, task_id, run_id):
    """Legacy diagnostic losses are not receipts; marked losses join the causal history."""
    from hermes_cli.kanban_failed_result_identity import _payload
    rows = conn.execute("SELECT * FROM task_events WHERE task_id=? AND run_id=? "
                        "AND kind IN ('worker_result','worker_bound','worker_lost') ORDER BY id",
                        (task_id, run_id)).fetchall()
    return [row for row in rows if row['kind'] != 'worker_lost'
            or (_payload(row) or {}).get('loss_receipt') is True]
