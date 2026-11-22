"""Stop-only attribution after the ordinary dead-interpreter proof has passed."""
from __future__ import annotations


def select_refusal(conn, row, proof):
    """A later callback/owner restart cannot erase an earlier attributable refusal.

    This never supplies process-death or retry authority: an ordinary reclaim
    proof is a prerequisite, and the refused binding must itself be proven gone.
    """
    if not proof:
        return None
    from hermes_cli.kanban_failed_result import process_gone
    from hermes_cli.kanban_failed_result_identity import _identity, _payload
    from hermes_cli.kanban_lost_result import evidence_rows
    from hermes_cli.kanban_refusal_receipt import refusal_fields

    events = evidence_rows(conn, row['id'], row['current_run_id'])
    bounds = [(e, _payload(e)) for e in events if e['kind'] == 'worker_bound']
    for event in events:
        p = _payload(event)
        if (event['kind'] != 'worker_result' or not p or not refusal_fields(p)
                or p.get('failure_retryable') is not False
                or type(p.get('exit_code')) is not int or p['exit_code'] == 0
                or p.get('claim_lock') != row['claim_lock']):
            continue
        for bound_event, bound in bounds:
            if not bound or bound.get('claim_lock') != row['claim_lock'] or p.get('pid') != bound.get('pid'):
                continue
            if p.get('death_proof_version') == 1:
                if p.get('bound_event_identity') != _identity(bound_event, bound):
                    continue
            elif p.get('death_proof_version') is not None or bound_event['id'] >= event['id']:
                continue
            if process_gone(bound.get('pid'), bound.get('started_at')):
                result = {**proof, 'source_result_event_id': event['id'], 'result': p}
                result.pop('source_result_event_uid', None)
                if 'uid' in event.keys() and event['uid']:
                    result['source_result_event_uid'] = event['uid']
                return result
    return None
