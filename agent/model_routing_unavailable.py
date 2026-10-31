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
