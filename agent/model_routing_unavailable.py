"""Recoverable subscription capacity refusals without entering legacy paid fallback."""
from agent.model_routing import ModelRoutingError
from agent.credential_pool_disabled import enabled_entries


class SubscriptionQuotaUnavailable(ModelRoutingError):
    """Policy is satisfied, but its OAuth pool cannot serve until a known reset."""

    status_code = 429

    def __init__(self, provider, model, resets_at, pool_resets_at=None):
        self.provider, self.model, self.resets_at = provider, model, resets_at
        # [daveey] ``resets_at`` is the nearest account's reset (the message, every waiter); ``pool_resets_at``
        # is when at least half the pool can serve again, which is what hold-or-move decisions read
        # (``model_routing_walls.decision_reset``). Hub 11-14: one 5 h-window account of seven made a
        # 10-156 h wall read as a 1.5 h one.
        self.pool_resets_at = resets_at if pool_resets_at is None else pool_resets_at
        message = f"Subscription quota unavailable for {provider}/{model}; resets at {resets_at}"
        self.body = {"error": {"type": "rate_limit_error", "message": message, "resets_at": resets_at}}
        super().__init__(message)


class SubscriptionAuthUnavailable(ModelRoutingError):
    """[daveey] The OAuth pool is empty because rows are auth-benched (401/revoked), not only quota-capped.

    Capacity is unknown, so it carries no reset: a worker retries soon (exit 75) without a quota
    episode or provider wall. 2026-11-12: 3 revoked rows + 4 opus-capped rows read as a 5-day wall."""

    status_code = 503

    def __init__(self, provider, model):
        self.provider, self.model = provider, model
        super().__init__(f"Subscription credentials for {provider}/{model} are auth-benched (401); "
                         "quota unconfirmed, retry after the pool re-reads its tokens")


class QuotaContradicted(SubscriptionAuthUnavailable):
    """[daveey] The pool read quota-walled, but the provider's live usage windows show an account with
    headroom (``hermes_cli.kanban_quota_probe.contradicted``): unconfirmed, so it carries no reset either."""

    def __init__(self, provider, model, detail):
        super().__init__(provider, model)
        self.args = (f"Subscription quota for {provider}/{model} unconfirmed: the pool is empty but live usage "
                     f"shows headroom ({str(detail)[:200]}); retrying without a quota deadline",)


def auth_benched(entry, model, now):
    """True for a row benched by a 401 whose own capacity for *model* is unknown (no model cap of its own)."""
    from agent.credential_pool import STATUS_EXHAUSTED
    from agent.credential_pool_model_cooldowns import model_cooldown_until
    if entry.last_status != STATUS_EXHAUSTED or entry.last_error_code != 401:
        return False
    return not ((model_cooldown_until(entry, model) or 0) > now)


def auth_churned(pool, model):
    """At least one live OAuth row of *pool* is benched only by a 401 (see ``auth_benched``)."""
    from agent.credential_pool import STATUS_DEAD
    import time
    if pool is None:
        return False
    now = time.time()
    return any(entry.auth_type == "oauth" and entry.last_status != STATUS_DEAD and auth_benched(entry, model, now)
               for entry in enabled_entries(pool.provider, pool.entries()))


def _quota_scan(pool, model):
    """``(room, waits)`` for *model*: room when a live OAuth entry can serve it now, else its quota resets.

    Every live OAuth entry must carry quota evidence: one on an auth/refresh/billing bench without its
    own model cooldown means "not proven a quota wall" (no waits), though it is not room either."""
    from agent.credential_pool import STATUS_DEAD, _exhausted_until
    from agent.credential_pool_model_cooldowns import MODEL_ENTITLEMENT_BENCH_SECONDS, model_cooldown_until
    import time

    if pool is None:
        return False, []
    now = time.time()
    # Disabled rows are neither usable capacity nor evidence of a quota/auth wall.
    entries = [entry for entry in enabled_entries(pool.provider, pool.entries())
               if entry.auth_type == "oauth" and entry.last_status != STATUS_DEAD]
    waits, unproven = [], False
    for entry in entries:
        account = _exhausted_until(entry, sole_credential=len(entries) <= 1)
        model_reset = model_cooldown_until(entry, model)
        if model_reset and model_reset - now >= MODEL_ENTITLEMENT_BENCH_SECONDS / 2:
            continue  # entitlement benches do not reopen on a quota reset
        if account and account > now and entry.last_error_code != 429:
            # An auth/refresh/billing bench is not evidence of quota: unless this account
            # also has its own cooldown for the model, the pool is not proven exhausted.
            if not model_reset:
                unproven = True
                continue
            account = None
        reset = max(account or 0, model_reset or 0)
        if reset <= now:
            return True, []  # not an exhausted pool
        waits.append(reset)
    return False, [] if unproven else waits


def quota_unavailable(pool, model):
    """Only a known cooldown is quota evidence; empty/dead OAuth pools are not."""
    room, waits = _quota_scan(pool, model)
    if waits and not room:
        return SubscriptionQuotaUnavailable(pool.provider, model, min(waits), half_pool_reset(waits))
    return None


# Slack after a known reset so the retry lands past the provider's own clock, not on its edge.
POOL_RESET_MARGIN_S = 3.0
# Longest known reset worth sleeping through: ``RETRY_AFTER_CAP_S`` plus slack for the margin, so a reset
# the 600 s cap would have missed by seconds (hub 11-20: 603 s away) is still waited for.
POOL_RESET_BUDGET_S = 600.0 + 60.0


def pool_reset_wait(pool, model, now=None):
    """``(seconds_until_earliest_reset, SubscriptionQuotaUnavailable)`` when every pooled OAuth
    credential is cooling down for *model* with a known reset, else None.

    A real ``CredentialPool`` only: anything else (a stub, no pool) carries no reset evidence."""
    from agent.credential_pool import CredentialPool
    import time

    if not isinstance(pool, CredentialPool):
        return None
    refusal = quota_unavailable(pool, model)
    if refusal is None:
        return None
    return refusal.resets_at - (time.time() if now is None else now), refusal


def half_pool_reset(waits):
    """The reset by which at least half of the walled accounts behind *waits* can serve again."""
    ordered = sorted(waits)
    return ordered[(len(ordered) + 1) // 2 - 1]


def quota_room(pool, model):
    """True only on positive evidence: a live OAuth entry with no quota or bench cooldown on *model* now.

    The converse of :func:`quota_unavailable` is not room: an empty, dead or auth-benched pool is neither."""
    return _quota_scan(pool, model)[0]


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


def quota_refusal_wall(exc):
    """``(provider, model, resets_at)`` when *exc*'s cause chain ends in one route's own quota refusal (the
    routing pool's ``SubscriptionQuotaUnavailable``, a pinned route's wall) naming its route and a finite
    absolute ``resets_at``, else None.

    A fallback chain gives None: its routes are the pin plus configured fallbacks, so no one route and reset
    describes it (a mixed chain's real cause may not be a quota at all). A ``retry_after`` hint is not an
    absolute reset. The names are as the refusal gives them, like ``model_routing_walls.held_reply``."""
    from hermes_cli.fallback_errors import ProviderFallbackError

    error = _terminal_refusal(exc)
    if error is None or isinstance(error, ProviderFallbackError) or not _is_quota_type(error):
        return None
    provider, model = getattr(error, "provider", None), getattr(error, "model", None)
    reset = _finite_positive(getattr(error, "resets_at", None))
    if reset is None or not all(isinstance(v, str) and v.strip() for v in (provider, model)):
        return None
    return provider.strip(), model.strip(), reset


def is_quota_failure(exc):
    """True when *exc*'s cause chain ends in a quota refusal (every fallback route one, too),
    whether or not it carries a usable deadline."""
    from hermes_cli.fallback_errors import ProviderFallbackError

    error = _terminal_refusal(exc)
    if isinstance(error, ProviderFallbackError):
        return bool(error.failures) and all(is_quota_failure(f) for _, _, f in error.failures)
    return error is not None and _is_quota_type(error)
