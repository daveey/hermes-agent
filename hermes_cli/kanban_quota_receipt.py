"""Typed quota deadlines retained in attempt receipts, not provider-wide guesses."""
import math


def quota_fields(failure):
    """Allowlisted, credential-free fields from a failed turn or startup exception."""
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from hermes_cli.auth import AuthError, is_rate_limited_auth_error
    import time
    seen = set()
    while isinstance(failure, BaseException) and id(failure) not in seen:
        seen.add(id(failure))
        if isinstance(failure, SubscriptionQuotaUnavailable):
            failure = {'failure_reason': 'rate_limit', 'failure_resets_at': failure.resets_at}
            break
        if isinstance(failure, AuthError) and is_rate_limited_auth_error(failure):
            retry = failure.retry_after
            if _finite(retry) and retry > 0:
                failure = {'failure_reason': 'rate_limit', 'failure_resets_at': time.time() + retry}
                break
        failure = failure.__cause__ or failure.__context__
    if not isinstance(failure, dict) or failure.get('failure_reason') not in {'rate_limit', 'upstream_rate_limit', 'billing'}:
        return {}
    reset = failure.get('failure_resets_at')
    if not _finite(reset):
        return {}
    return {'failure_reason': 'rate_limit', 'failure_retryable': True, 'failure_resets_at': reset}


_UNIT_SECONDS = {'d': 86400, 'day': 86400, 'days': 86400, 'h': 3600, 'hr': 3600, 'hrs': 3600, 'hour': 3600,
                 'hours': 3600, 'm': 60, 'min': 60, 'mins': 60, 'minute': 60, 'minutes': 60, 's': 1, 'sec': 1,
                 'secs': 1, 'second': 1, 'seconds': 1}
_TERM = r'(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])'


def _finite(value):
    """A real int/float that is finite; a huge int (``isfinite`` overflows) is not."""
    if type(value) not in (float, int):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def valid_deadline(value):
    """A finite epoch a datetime can represent, else None: malformed hints are rejected, never clamped."""
    from datetime import UTC, datetime
    if not _finite(value):
        return None
    try:
        datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return float(value)


def _relative_seconds(text):
    """Longest ``resets in`` duration, summing compound terms (``2h 30m``), so no part is dropped early."""
    import re
    found = []
    for match in re.finditer(r'resets?\s+in\s+~?\s*((?:' + _TERM + r'[\s,]*)+)', text, re.IGNORECASE):
        found.append(sum(float(n) * _UNIT_SECONDS[u.lower()]
                         for n, u in re.findall(_TERM, match[1], re.IGNORECASE)))
    return max(found) if found else None


def _owned_receipt(conn, task_id, run_id):
    """The exact attempt's quota-exit receipt under its own claim, or None."""
    from hermes_cli import kanban_db as kb
    row = conn.execute(
        "SELECT e.payload, e.created_at, r.claim_lock FROM task_events e JOIN task_runs r "
        "ON r.id=e.run_id AND r.task_id=e.task_id WHERE e.task_id=? AND e.run_id=? "
        "AND e.kind='worker_result' ORDER BY e.id DESC LIMIT 1", (task_id, run_id)).fetchone()
    if row is None:
        return None
    receipt = kb._json_dict(row['payload'])
    if (receipt.get('exit_code') != kb.KANBAN_RATE_LIMIT_EXIT_CODE
            or (row['claim_lock'] and receipt.get('claim_lock') != row['claim_lock'])):
        return None
    return receipt, kb._to_epoch(row['created_at'])


def receipt_deadline(conn, task_id, run_id):
    """Typed first, then legacy text, anchored to the exact receipt's timestamp."""
    from datetime import datetime
    import re
    owned = _owned_receipt(conn, task_id, run_id)
    if owned is None:
        return None
    receipt, created = owned
    reset = valid_deadline(quota_fields(receipt).get('failure_resets_at'))
    if reset is not None:
        return reset
    text = str(receipt.get('last_output') or '')
    if created is None:
        return None
    relative = _relative_seconds(text)
    if relative is not None:
        return valid_deadline(created + relative)
    absolute = re.search(r'resets?\s+(?:at|on)\s+(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:\d\d))', text, re.IGNORECASE)
    if absolute:
        try:
            return valid_deadline(datetime.fromisoformat(absolute[1]).timestamp())
        except (ValueError, OverflowError):
            return None
    # Pool refusals carry ``resets at <epoch>`` (SubscriptionQuotaUnavailable's text).
    epoch = re.search(r'resets?\s+at\s+(\d{9,11}(?:\.\d+)?)(?![\d:])', text, re.IGNORECASE)
    if epoch:
        return valid_deadline(float(epoch[1]))
    from hermes_cli.kanban_provider_walls import parse_retry_after
    retry = parse_retry_after(text)
    if retry is not None:
        return valid_deadline(created + retry)
    return None


def _route_name(value):
    return value if isinstance(value, str) and value and value.strip() == value and value != 'auto' else None


def route_fields(failure, agent=None):
    """The concrete provider/model that actually ran (or was refused), for the attempt receipt.

    A class selector's admission receipt cannot know it (the worker allocates); the exhausted
    pool's typed refusal or the live agent's runtime can. Unknown stays absent, never guessed."""
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    seen = set()
    while isinstance(failure, BaseException) and id(failure) not in seen:
        seen.add(id(failure))
        if isinstance(failure, SubscriptionQuotaUnavailable):
            provider, model = _route_name(failure.provider), _route_name(failure.model)
            return {'route_provider': provider, 'route_model': model} if provider else {}
        failure = failure.__cause__ or failure.__context__
    provider = _route_name(getattr(agent, 'provider', None))
    if not provider:
        return {}
    return {'route_provider': provider, 'route_model': _route_name(getattr(agent, 'model', None))}


def receipt_route(conn, task_id, run_id):
    """``route_provider`` recorded by the exact attempt's own quota receipt, or None."""
    owned = _owned_receipt(conn, task_id, run_id)
    return _route_name(owned[0].get('route_provider')) if owned else None
