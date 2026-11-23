"""Credential-free, typed safety evidence for an attributable worker result.

This is stop evidence for the refusing provider, not clearance of the work. The card is retried
unchanged on another eligible provider (``kanban_refusal_fallback``) until every one has refused; only then is it
held with the safety restriction below. Never classify a refusal from last_output: it can quote a refusal from an
unrelated attempt.
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

    Primary closure belongs solely to the canonical release/feed. Local boards end the run atomically and
    either return the card to ``ready`` for an unchanged run on another eligible provider (no restriction), or,
    when none is left or the refusing provider is unproven, retain the same decision kernel as before.
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

    task, run = row['id'], row['current_run_id']
    diagnostic = {k: v for k, v in proof.items() if k != 'result'}
    diagnostic.update(refusal_fields(result))
    from hermes_cli import kanban_refusal_fallback as fallback
    plan = fallback.plan(conn, row, result)
    if plan['refused_provider']:
        diagnostic['refused_provider'] = plan['refused_provider']
    if plan['refused_providers']:
        diagnostic['refused_providers'] = plan['refused_providers']
    if plan['next']:
        # Board authority closes a primary run; this is the core side of the contract (see fallback docs).
        diagnostic['safety_fallback'] = {'next_model': plan['next'][0], 'next_provider': plan['next'][1]}
    if release.queue(conn, task, run, outcome='crashed',
                     error='Provider safety refusal; execution release unavailable', diagnostic=diagnostic):
        return True
    # Revalidate the exact admitted run; no state from a later attempt is changed.
    from hermes_cli.kanban_source_receipt import with_source_receipt
    # A fallback card is ready and unrestricted, so it carries no failure text (nothing was charged).
    status, error = ('ready', None) if plan['next'] else ('triage', 'Provider safety refusal; execution release unavailable')
    changed = with_source_receipt(conn, task, lambda: conn.execute(
        "UPDATE tasks SET status=?, claim_lock=NULL, claim_expires=NULL, "
        "worker_pid=NULL, worker_started_at=NULL, last_failure_error=? "
        "WHERE id=? AND status='running' AND current_run_id=? AND claim_lock IS ? "
        "AND worker_pid IS ? AND worker_started_at IS ?",
        (status, error, task, run, row['claim_lock'], row['worker_pid'], row['worker_started_at'])), run_id=run)
    if changed.rowcount != 1:
        return True  # lost CAS: never book a competing failure
    if plan['next']:
        fallback.requeue(conn, row, plan, diagnostic)
        return True
    diagnostic.pop('safety_fallback', None)
    payload = {**diagnostic, 'cause': 'safety_refusal', 'accounting_boundary': False,
               'reason': 'Provider safety refusal; execution release unavailable',
               'release_authority': 'unavailable',
               'next_step': 'Owning supervisor must resolve the scope/authority boundary; do not rerun refused work'}
    kb._end_run(conn, task, outcome='safety_refused', status='safety_refused',
                error=payload['reason'], metadata=payload)
    fallback._triage(conn, task, run, payload)
    return True
