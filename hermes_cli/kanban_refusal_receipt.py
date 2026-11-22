"""Credential-free, typed safety evidence for an attributable worker result.

This is stop evidence, not provider clearance or a retry grant. Never classify a
refusal from last_output: it can quote a refusal from an unrelated attempt.
"""
from __future__ import annotations


def refusal_fields(result) -> dict:
    """Keep the terminal turn's verdict, excluding provider prose and transcript."""
    if (not isinstance(result, dict) or result.get('failed') is not True
            or result.get('failure_reason') != 'content_policy_blocked'):
        return {}
    from agent.turn_failure_copy import safe_refusal_category

    fields = {'failed': True, 'failure_reason': 'content_policy_blocked',
              'failure_retryable': False,
              'refused_turn_ran_actions': result.get('refused_turn_ran_actions') is True}
    category = safe_refusal_category(result.get('refusal_category'))
    if category is not None:
        fields['refusal_category'] = category
    return fields


def restricted(conn, task_id: str) -> bool:
    """A safety restriction cannot be cleared by ordinary budget promotion/archive."""
    from hermes_cli.kanban_spend_decision import receipt_restrictions
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind IN "
        "('spend_budget_triaged','decision_hold_restricted') AND json_valid(payload) "
        "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.cause') END='safety_refusal' "
        "LIMIT 1", (task_id,)).fetchone() is not None or bool(receipt_restrictions(conn, task_id, safety_only=True))


def reclaim_admission(conn, row, *, failed_results_only=False):
    """Existing death/owner admission followed by stop-before-retry finalization."""
    from hermes_cli.kanban_failed_result import reclaim_proof
    from hermes_cli.kanban_owner_recovery import owner_reclaim_blocked
    proof, hold = reclaim_proof(conn, row, failed_results_only=failed_results_only)
    if hold or (proof is None and owner_reclaim_blocked(conn, row['id'], crash_sweep=True)):
        return proof, True
    return proof, stop_failed_refusal(conn, row, proof)


def stop_failed_refusal(conn, row, proof) -> bool:
    """Consume an exact dead-interpreter proof inside the reclaim transaction.

    Primary closure belongs solely to the canonical release/feed. Local boards
    end the run and retain the same decision kernel atomically, without retry.
    """
    from hermes_cli.kanban_refusal_proof import select_refusal
    proof = select_refusal(conn, row, proof)
    if not proof:
        return False
    result = proof['result']
    if type(result.get('exit_code')) is not int or result['exit_code'] == 0:
        return False
    if not conn.in_transaction:
        raise RuntimeError('refusal finalization requires the reclaim transaction')
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_failure_release as release
    from hermes_cli import kanban_spend_decision as decision

    task, run = row['id'], row['current_run_id']
    diagnostic = {k: v for k, v in proof.items() if k != 'result'}
    diagnostic.update(refusal_fields(result))
    if release.queue(conn, task, run, outcome='crashed',
                     error='Provider safety refusal; execution release unavailable', diagnostic=diagnostic):
        return True
    # Revalidate the exact admitted run; no state from a later attempt is changed.
    from hermes_cli.kanban_source_receipt import with_source_receipt
    changed = with_source_receipt(conn, task, lambda: conn.execute(
        "UPDATE tasks SET status='triage', claim_lock=NULL, claim_expires=NULL, "
        "worker_pid=NULL, worker_started_at=NULL, "
        "last_failure_error='Provider safety refusal; execution release unavailable' "
        "WHERE id=? AND status='running' AND current_run_id=? AND claim_lock IS ? "
        "AND worker_pid IS ? AND worker_started_at IS ?",
        (task, run, row['claim_lock'], row['worker_pid'], row['worker_started_at'])), run_id=run)
    if changed.rowcount != 1:
        return True  # lost CAS: never book a competing failure
    payload = {**diagnostic, 'cause': 'safety_refusal', 'accounting_boundary': False,
               'reason': 'Provider safety refusal; execution release unavailable',
               'release_authority': 'unavailable',
               'next_step': 'Owning supervisor must resolve the scope/authority boundary; do not rerun refused work'}
    kb._end_run(conn, task, outcome='safety_refused', status='safety_refused',
                error=payload['reason'], metadata=payload)
    if (stop := decision.pending_episode(conn, task)) is not None:
        payload['episode'] = stop.get('uid') or str(stop['id'])
        kb._append_event(conn, task, 'decision_hold_restricted', payload, run_id=run)
    else:
        kb._append_event(conn, task, decision.EVENT, payload, run_id=run)
    kb._append_event(conn, task, 'safety_refused', payload, run_id=run)
    decision.safe_ensure_producer(conn, task)
    return True
