"""Recoverable subscription capacity refusals without entering legacy paid fallback."""
from agent.model_routing import ModelRoutingError


class SubscriptionQuotaUnavailable(ModelRoutingError):
    """Policy is satisfied, but its OAuth pool cannot serve until a known reset."""

    status_code = 429

    def __init__(self, provider, model, resets_at):
        self.provider, self.model, self.resets_at = provider, model, resets_at
        message = f"Subscription quota unavailable for {provider}/{model}; resets at {resets_at}"
        self.body = {"error": {"type": "rate_limit_error", "message": message, "resets_at": resets_at}}
        super().__init__(message)


def quota_unavailable(pool, model):
    """Only a known cooldown is quota evidence; empty/dead OAuth pools are not."""
    from agent.credential_pool import STATUS_DEAD, _exhausted_until
    from agent.credential_pool_model_cooldowns import MODEL_ENTITLEMENT_BENCH_SECONDS, model_cooldown_until
    import time

    if pool is None:
        return None
    now = time.time()
    entries = [entry for entry in pool.entries() if entry.auth_type == "oauth" and entry.last_status != STATUS_DEAD]
    waits = []
    for entry in entries:
        account = _exhausted_until(entry, sole_credential=len(entries) <= 1)
        if account and account > now and entry.last_error_code != 429:
            continue  # an auth/refresh/billing bench is not evidence of quota
        model_reset = model_cooldown_until(entry, model)
        if model_reset and model_reset - now >= MODEL_ENTITLEMENT_BENCH_SECONDS / 2:
            continue  # entitlement benches do not reopen on a quota reset
        reset = max(account or 0, model_reset or 0)
        if reset <= now:
            return None  # not an exhausted pool
        waits.append(reset)
    if waits:
        return SubscriptionQuotaUnavailable(pool.provider, model, min(waits))
    return None


def _finite_positive(value):
    """A real int/float, finite and > 0 (huge ints and NaN/inf are not a deadline)."""
    import math

    if type(value) not in (float, int):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) and value > 0 else None


def _is_quota_type(error):
    """True for an exception whose own type says "quota window closed" (not its text)."""
    from hermes_cli.auth import AuthError, is_rate_limited_auth_error

    if isinstance(error, ModelRoutingError):
        return getattr(error, "status_code", None) == 429
    return isinstance(error, AuthError) and is_rate_limited_auth_error(error)


def _own_wait(error, now):
    """Seconds this one refusal says it stays closed: an absolute ``resets_at`` (pool refusal) or a
    ``retry_after`` / ``retry after <N>s`` hint (Codex 429 AuthError)."""
    import re

    reset = _finite_positive(getattr(error, "resets_at", None))
    if reset is not None:
        return _finite_positive(reset - now)
    hint = _finite_positive(getattr(error, "retry_after", None))
    if hint is None and not isinstance(error, ModelRoutingError):
        m = re.search(r"retry after (\d+)s", str(error), re.IGNORECASE)
        hint = _finite_positive(float(m.group(1))) if m else None
    return hint


def _route_wait(error, now):
    from hermes_cli.fallback_errors import ProviderFallbackError

    if isinstance(error, ProviderFallbackError):
        waits = [_route_wait(failure, now) for _, _, failure in error.failures]
        return min(waits) if waits and all(w is not None for w in waits) else None
    return _own_wait(error, now) if _is_quota_type(error) else None


def _terminal_refusal(exc):
    """First quota refusal or fallback chain in *exc*'s cause chain, else None. A fallback's
    implicit context may be the primary's refusal: the chain decides, not that context."""
    from hermes_cli.auth import AuthError
    from hermes_cli.fallback_errors import ProviderFallbackError

    seen = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, (ProviderFallbackError, AuthError)) or _is_quota_type(cur):
            return cur
        cur = cur.__cause__ or cur.__context__
    return None


def quota_wait_seconds(exc, now=None):
    """Seconds until the quota window that refused *exc* reopens, or None.

    The one mapping from every quota refusal type the runtime raises (the routing pool's
    ``SubscriptionQuotaUnavailable`` or any other 429 ``ModelRoutingError``, the Codex rate-limited
    ``AuthError``, a ``ProviderFallbackError`` whose every route is one of those) to a wait.
    Consumers (cron's quota hold) call this instead of keeping their own isinstance lists, so a new
    quota type cannot silently bypass them. Typed only: failure text never yields a wait."""
    import time

    error = _terminal_refusal(exc)
    return None if error is None else _route_wait(error, time.time() if now is None else now)


def quota_refusal_route(exc):
    """``(provider, model)`` of the route whose quota refusal ended *exc*'s cause chain, else None.

    The pool refusal names its own route; for a fallback chain it is the terminal (last-tried)
    route. A refusal that does not name both (the Codex ``AuthError`` carries no model) gives None.
    Typed only, like ``quota_wait_seconds``."""
    from hermes_cli.fallback_errors import ProviderFallbackError

    error = _terminal_refusal(exc)
    if isinstance(error, ProviderFallbackError):
        if not error.failures:
            return None
        provider, model, _ = error.failures[-1]
    elif error is not None and _is_quota_type(error):
        provider, model = getattr(error, "provider", None), getattr(error, "model", None)
    else:
        return None
    if not (isinstance(provider, str) and provider.strip() and isinstance(model, str) and model.strip()):
        return None
    return provider.strip().lower(), model.strip()


def is_quota_failure(exc):
    """True when *exc*'s cause chain ends in a quota refusal (every fallback route one, too),
    whether or not it carries a usable deadline."""
    from hermes_cli.fallback_errors import ProviderFallbackError

    error = _terminal_refusal(exc)
    if isinstance(error, ProviderFallbackError):
        return bool(error.failures) and all(is_quota_failure(f) for _, _, f in error.failures)
    return error is not None and _is_quota_type(error)
