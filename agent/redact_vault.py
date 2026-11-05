"""Profile-scoped, bounded exact-value redaction for vault-filled secrets."""

import threading

# Memory only: never persist or log vault values. Profiles must not reveal each
# other's values, and a fill-heavy session must not grow the registry forever.
_VAULT_REDACTION_MAX_PER_PROFILE = 64
_VAULT_REDACTION_VALUES: dict = {}
_VAULT_REDACTION_LOCK = threading.Lock()


def _vault_scope() -> str:
    from hermes_constants import get_hermes_home
    return str(get_hermes_home())


def register_vault_redaction_value(value) -> None:
    """Register before injection, including the CR/LF-stripped input value."""
    if not isinstance(value, str) or not value:
        return
    normalized = value.replace("\r", "").replace("\n", "")
    with _VAULT_REDACTION_LOCK:
        bucket = _VAULT_REDACTION_VALUES.setdefault(_vault_scope(), {})
        for v in (value, normalized):
            if v:
                bucket.pop(v, None)  # re-registering refreshes recency
                bucket[v] = None
        while len(bucket) > _VAULT_REDACTION_MAX_PER_PROFILE:
            del bucket[next(iter(bucket))]


def clear_vault_redaction_values() -> None:
    """Drop the current profile's registered values on teardown or lock."""
    with _VAULT_REDACTION_LOCK:
        _VAULT_REDACTION_VALUES.pop(_vault_scope(), None)


def redact_registered_vault_values(text: str) -> str:
    """Scrub longest values first so a substring never shadows its superstring."""
    if not isinstance(text, str) or not text:
        return text
    with _VAULT_REDACTION_LOCK:
        bucket = _VAULT_REDACTION_VALUES.get(_vault_scope())
        values = sorted(bucket, key=len, reverse=True) if bucket else ()
    for value in values:
        if value in text:
            text = text.replace(value, "«redacted-vault-secret»")
    return text
