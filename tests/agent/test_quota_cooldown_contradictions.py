"""Captured weekly rejection and contradictory model cooldown regressions."""
from types import SimpleNamespace

import pytest

from agent.request_bucket_context import request_bucket_context

from tests.agent import test_anthropic_model_cooldowns as helpers


@pytest.mark.parametrize('retry_after', ['NaN', 'Infinity', '-Infinity', float('nan'), float('inf')])
def test_nonfinite_request_retry_falls_back_to_request_reset(retry_after):
    headers = {'retry-after': retry_after, 'anthropic-ratelimit-requests-remaining': '0',
               'anthropic-ratelimit-requests-reset': str(CAPTURED_AT + 7)}
    context = {'reset_at': 1794672000, 'message': MESSAGE}
    assert request_bucket_context(_error(headers), context, now=CAPTURED_AT) == {
        **context, 'reset_at': CAPTURED_AT + 7}

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


@pytest.mark.parametrize('reset', ['NaN', 'Infinity', '-Infinity', float('nan'), float('inf')])
def test_nonfinite_request_reset_preserves_context(reset):
    context = {'reset_at': 1794672000, 'reason': 'weekly', 'message': MESSAGE}
    error = _error({'anthropic-ratelimit-requests-remaining': '0',
                    'anthropic-ratelimit-requests-reset': reset})
    assert request_bucket_context(error, context, now=CAPTURED_AT) is context


@pytest.mark.parametrize('remaining_header', [
    'anthropic-ratelimit-requests-remaining', 'X-RateLimit-Remaining-Requests'])
@pytest.mark.parametrize('retry_after', ['2', '0', '2.5'])
def test_header_only_request_retry_preserves_other_context(remaining_header, retry_after):
    context = {'reason': 'rate_limit', 'message': MESSAGE, 'sentinel': object()}
    original = dict(context)
    headers = {remaining_header: ' 0 ', 'ReTrY-AfTeR': retry_after,
               'anthropic-ratelimit-requests-reset': str(CAPTURED_AT + 100)}
    result = request_bucket_context(_error(headers), context, now=CAPTURED_AT)
    assert result == {**context, 'reset_at': CAPTURED_AT + float(retry_after)}
    assert result is not context
    assert context == original


@pytest.mark.parametrize('retry_after', [None, '', 'garbage', True])
@pytest.mark.parametrize('reset', [1794511821, '1794511821', '1794511821000',
                                   '2026-11-12T19:30:21Z'])
def test_request_reset_fallback(retry_after, reset):
    context = {'message': MESSAGE, 'reset_at': 1794672000}
    headers = {'retry-after': retry_after, 'anthropic-ratelimit-requests-remaining': '0',
               'anthropic-ratelimit-requests-reset': reset}
    assert request_bucket_context(_error(headers), context, now=CAPTURED_AT) == {
        **context, 'reset_at': 1794511821}


@pytest.mark.parametrize('reset', [None, '', 'garbage', '2026-99-99', 0, -1, CAPTURED_AT - 1])
def test_invalid_or_past_request_reset_preserves_context(reset):
    context = {'reset_at': 1794672000, 'message': MESSAGE}
    headers = {'retry-after': 'garbage', 'anthropic-ratelimit-requests-remaining': '0',
               'anthropic-ratelimit-requests-reset': reset}
    assert request_bucket_context(_error(headers), context, now=CAPTURED_AT) is context


@pytest.mark.parametrize('headers', [None, {}, {'retry-after': '2'},
    {'retry-after': '2', 'anthropic-ratelimit-requests-remaining': '1'},
    {'retry-after': '2', 'anthropic-ratelimit-requests-remaining': 'garbage'},
    {'retry-after': '2', 'anthropic-ratelimit-tokens-remaining': '0'},
    {'retry-after': '2', 'anthropic-ratelimit-requests-remaining': '-1'}])
def test_no_positive_request_bucket_evidence_preserves_context(headers):
    context = {'reset_at': 1794672000, 'message': MESSAGE}
    assert request_bucket_context(_error(headers), context, now=CAPTURED_AT) is context


@pytest.mark.parametrize('status', [None, 200, 401, 403, 500, '429'])
def test_non_429_preserves_context(status):
    error = _error({'retry-after': '2', 'anthropic-ratelimit-requests-remaining': '0'})
    error.status_code = status
    context = {'reset_at': 1794672000}
    assert request_bucket_context(error, context, now=CAPTURED_AT) is context


def test_missing_response_preserves_context():
    context = {'reset_at': 1794672000}
    error = SimpleNamespace(status_code=429)
    assert request_bucket_context(error, context, now=CAPTURED_AT) is context


@pytest.mark.parametrize('status_header', ['Anthropic-Ratelimit-Unified-Status',
    'anthropic-ratelimit-unified-7d-status', 'anthropic-ratelimit-unified-5h-status'])
def test_rejected_unified_window_wins_over_short_request_bucket(status_header):
    context = {'reset_at': 1794672000, 'message': MESSAGE}
    headers = {status_header: 'ReJeCtEd', 'retry-after': '2',
               'anthropic-ratelimit-requests-remaining': '0'}
    assert request_bucket_context(_error(headers), context, now=CAPTURED_AT) is context


@pytest.mark.parametrize('status_header, status', [
    ('anthropic-ratelimit-unified-status', 'allowed'),
    ('anthropic-ratelimit-unified-status', 'allowed_warning'),
    ('other-status', 'rejected'),
    ('anthropic-ratelimit-unified-representative-claim', 'rejected')])
def test_nonrejected_unified_window_does_not_mask_request_bucket(status_header, status):
    context = {'reset_at': 1794672000}
    headers = {status_header: status, 'retry-after': '2',
               'anthropic-ratelimit-requests-remaining': '0'}
    assert request_bucket_context(_error(headers), context, now=CAPTURED_AT) == {
        'reset_at': CAPTURED_AT + 2}


def test_header_only_extraction_has_short_reset(monkeypatch):
    from agent.agent_runtime_helpers import extract_api_error_context
    monkeypatch.setattr('time.time', lambda: CAPTURED_AT)
    error = _error({'retry-after': '2', 'anthropic-ratelimit-requests-remaining': '0'})
    assert extract_api_error_context(error)['reset_at'] == CAPTURED_AT + 2


def test_nonfinite_retry_extraction_uses_valid_request_reset(monkeypatch):
    from agent.agent_runtime_helpers import extract_api_error_context
    monkeypatch.setattr('time.time', lambda: CAPTURED_AT)
    error = _error({'retry-after': 'NaN', 'anthropic-ratelimit-requests-remaining': '0',
                    'anthropic-ratelimit-requests-reset': str(CAPTURED_AT + 7)},
                   reset_at=1794672000)
    assert extract_api_error_context(error)['reset_at'] == CAPTURED_AT + 7


def test_http_date_request_retry(monkeypatch):
    from datetime import datetime, UTC
    from agent import retry_utils

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(int(CAPTURED_AT), UTC)

    monkeypatch.setattr(retry_utils, 'datetime', FrozenDateTime)
    error = _error({'retry-after': 'Thu, 12 Nov 2026 19:30:21 GMT',
                    'anthropic-ratelimit-requests-remaining': '0'})
    assert request_bucket_context(error, {}, now=int(CAPTURED_AT)) == {'reset_at': 1794511821}


def test_exhausted_bucket_without_reset_preserves_context():
    context = {'reset_at': 1794672000}
    error = _error({'anthropic-ratelimit-requests-remaining': '0'})
    assert request_bucket_context(error, context, now=CAPTURED_AT) is context


def test_weekly_and_short_request_headers_keep_weekly_pool_bench(pool, monkeypatch):
    from agent.agent_runtime_helpers import extract_api_error_context
    monkeypatch.setattr('time.time', lambda: CAPTURED_AT)
    headers = {**WEEKLY_HEADERS, 'retry-after': '2',
               'anthropic-ratelimit-requests-remaining': '0'}
    context = extract_api_error_context(_error(headers, reset_at=1794672000))
    assert _bench(pool, context) == 1794672000


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
