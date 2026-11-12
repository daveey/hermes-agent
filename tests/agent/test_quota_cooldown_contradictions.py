"""Captured weekly rejection and contradictory model cooldown regressions."""
from types import SimpleNamespace

from tests.agent import test_anthropic_model_cooldowns as helpers

pool = helpers.pool
MODEL = helpers.MODEL_A
KEY = helpers.KEY
CAPTURED_AT = 1794511819.8353467
# Allowlisted headers of a live 1-token request, 2026-11-12 19:30:19Z.
# The generic body is NOT evidence of a short throttle: these headers prove a weekly wall.
WEEKLY_HEADERS = {
    'anthropic-ratelimit-unified-representative-claim': 'seven_day',
    'anthropic-ratelimit-unified-reset': '1794672000',
    'anthropic-ratelimit-unified-7d-reset': '1794672000',
    'anthropic-ratelimit-unified-7d-status': 'rejected',
    'anthropic-ratelimit-unified-7d-utilization': '1.0',
    'anthropic-ratelimit-unified-status': 'rejected',
    'retry-after': '160180',
}
MESSAGE = "This request would exceed your account's rate limit. Please try again later."


def _error(headers, **body_fields):
    error = RuntimeError(MESSAGE)
    error.body = {'error': {'type': 'rate_limit_error', 'message': MESSAGE, **body_fields}}
    error.response = SimpleNamespace(headers=headers)
    error.status_code = 429
    return error


def _bench(pool, context):
    pool.mark_exhausted_and_rotate(status_code=429, error_context=context,
                                   api_key_hint=KEY, failure_reason='rate_limit', model=MODEL)
    return pool.entries()[0].model_cooldowns[MODEL]


def test_captured_generic_message_with_weekly_headers_keeps_weekly_bench(pool, monkeypatch):
    from agent.agent_runtime_helpers import extract_api_error_context
    monkeypatch.setattr('time.time', lambda: CAPTURED_AT)
    until = _bench(pool, extract_api_error_context(_error(WEEKLY_HEADERS)))
    assert until == CAPTURED_AT + 160180


def test_request_bucket_retry_after_not_usage_reset(pool, monkeypatch):
    """Synthetic conflicting fields, not a claim that the captured response had these headers."""
    from agent.agent_runtime_helpers import extract_api_error_context
    monkeypatch.setattr('time.time', lambda: CAPTURED_AT)
    headers = {'retry-after': '2', 'anthropic-ratelimit-requests-remaining': '0',
               'anthropic-ratelimit-requests-reset': '2026-11-12T19:30:21Z'}
    until = _bench(pool, extract_api_error_context(_error(headers, reset_at=1794672000)))
    assert 0 < until - CAPTURED_AT <= 2


def test_external_model_reset_is_seen_by_existing_pool(pool):
    from hermes_cli.auth import read_credential_pool, write_credential_pool
    import time
    _bench(pool, {'reset_at': time.time() + 172800, 'message': MESSAGE})
    rows = read_credential_pool('anthropic')
    rows[0]['model_cooldowns'] = {}
    rows[0]['status_cleared_at'] = time.time()
    write_credential_pool('anthropic', rows, status_cleared_ids=[rows[0]['id']])
    assert pool.select(model=MODEL) is not None


def test_stale_flush_does_not_resurrect_externally_cleared_model_bench(pool):
    from hermes_cli.auth import read_credential_pool, write_credential_pool
    import time
    _bench(pool, {'reset_at': time.time() + 172800, 'message': MESSAGE})
    rows = read_credential_pool('anthropic')
    rows[0]['model_cooldowns'] = {}
    rows[0]['status_cleared_at'] = time.time()
    write_credential_pool('anthropic', rows, status_cleared_ids=[rows[0]['id']])
    pool._persist()
    assert not read_credential_pool('anthropic')[0].get('model_cooldowns')
