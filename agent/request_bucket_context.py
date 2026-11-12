"""Keep a request-bucket reset separate from subscription usage windows."""
from __future__ import annotations

import math

from agent.credential_pool_timestamps import _parse_absolute_timestamp
from agent.retry_utils import parse_retry_after_seconds


def request_bucket_context(error, context, *, now):
    """Override generic/body resets only with positive request-bucket evidence.

    Anthropic uses the same generic rate-limit message for a real weekly rejection;
    never classify that message alone as a transient throttle. Unified rejected
    windows stay authoritative even if request-bucket headers are also present.
    """
    if getattr(error, 'status_code', None) != 429:
        return context
    headers = getattr(getattr(error, 'response', None), 'headers', None) or {}
    headers = {str(k).lower(): v for k, v in headers.items()}
    if any(str(value).lower() == 'rejected' for key, value in headers.items()
           if key.startswith('anthropic-ratelimit-unified') and key.endswith('status')):
        return context
    exhausted = any(str(headers.get(key, '')).strip() == '0' for key in (
        'anthropic-ratelimit-requests-remaining', 'x-ratelimit-remaining-requests'))
    if not exhausted:
        return context
    # Validate before the shared parser clamps NaN / -Infinity to zero.
    raw_retry = headers.get('retry-after')
    try:
        finite_retry = math.isfinite(float(raw_retry))
    except (TypeError, ValueError):
        finite_retry = True  # HTTP-date or malformed: let the shared parser decide.
    seconds = parse_retry_after_seconds(headers) if finite_retry else None
    if seconds is None:
        reset = _parse_absolute_timestamp(headers.get('anthropic-ratelimit-requests-reset'))
        seconds = reset - now if reset is not None else None
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return context
    return {**context, 'reset_at': now + seconds}
