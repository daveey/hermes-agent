"""Typed quota deadlines retained in attempt receipts, not provider-wide guesses."""
import math


def quota_fields(failure):
    """Allowlisted, credential-free fields from a failed turn or startup exception."""
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    seen = set()
    while isinstance(failure, BaseException) and id(failure) not in seen:
        seen.add(id(failure))
        if isinstance(failure, SubscriptionQuotaUnavailable):
            failure = {'failure_reason': 'rate_limit', 'failure_resets_at': failure.resets_at}
            break
        failure = failure.__cause__ or failure.__context__
    if not isinstance(failure, dict) or failure.get('failure_reason') != 'rate_limit':
        return {}
    reset = failure.get('failure_resets_at')
    if type(reset) not in (float, int) or not math.isfinite(reset):
        return {}
    return {'failure_reason': 'rate_limit', 'failure_retryable': True, 'failure_resets_at': reset}


def waiting_for_quota_reset(conn, task_id, now):
    """Only the latest ended attempt's receipt can defer its next admission."""
    from hermes_cli import kanban_db as kb
    row = conn.execute(
        "SELECT e.payload FROM task_runs r JOIN task_events e ON e.run_id=r.id "
        "AND e.task_id=r.task_id AND e.kind='worker_result' "
        "WHERE r.id=(SELECT id FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC,id DESC LIMIT 1) AND r.outcome='rate_limited' "
        "ORDER BY e.id DESC LIMIT 1", (task_id,)).fetchone()
    if row is None:
        return False
    receipt = kb._json_dict(row[0])
    fields = quota_fields(receipt)
    return receipt.get('exit_code') == kb.KANBAN_RATE_LIMIT_EXIT_CODE and fields.get('failure_resets_at', 0) > now
