"""A pool emptied by revoked/401 rows is auth churn, never a typed quota reset. [daveey]

2026-11-12 11:30Z: the hub's refresh revoked the tokens workers still held. Each worker's pool then
held 3 rows ``exhausted(401)`` (accounts with weekly headroom) and 4 rows genuinely opus-capped
(429). ``quota_unavailable`` skipped the 401 rows and typed the pool as quota-walled until the
latest 429 reset (11-17), so every retry wrote a multi-day ``quota_episode`` and an ``anthropic``
provider wall although three accounts could serve.
"""
import json
import time

import pytest

from tests.agent import test_managed_subscription as fixtures

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route

MODEL = 'anthropic-deep'
AUTH = ['gmail', 'softmax', 'admin']
CAPPED = ['dvd', 'feynman', 'astar', 'bb']


@pytest.fixture
def pool_home(managed, tmp_path):
    entries = [{'id': 'paid', 'label': 'paid', 'priority': 0, 'auth_type': 'api_key', 'source': 'manual',
                'access_token': 'sk-ant...paid'}]
    for n, name in enumerate(AUTH + CAPPED, start=1):
        entries.append({'id': name, 'label': name, 'priority': n, 'auth_type': 'oauth', 'source': 'manual',
                        'access_token': f'sk-ant-oat01-fixture-{name}'})
    (tmp_path / 'auth.json').write_text(json.dumps({'version': 1, 'credential_pool': {'anthropic': entries}}),
                                        encoding='utf-8')
    return managed


def _mark(*, revoked=AUTH, capped=CAPPED, reset=None):
    from agent.credential_pool import load_pool
    pool = load_pool('anthropic', allowed_auth_types={'oauth'})
    reset = reset or time.time() + 5 * 86400
    for name in capped:
        pool.mark_exhausted_and_rotate(status_code=429, model=MODEL, credential_id=name,
                                       error_context={'reset_at': reset}, failure_reason='rate_limit')
    for name in revoked:
        pool.mark_exhausted_and_rotate(status_code=401, model=MODEL, credential_id=name,
                                       error_context={'message': 'OAuth access token has been revoked'})
    return load_pool('anthropic', allowed_auth_types={'oauth'}), reset


def test_revoked_401_rows_plus_real_429s_are_not_a_quota_wall(pool_home):
    from agent.model_routing_unavailable import quota_unavailable
    from hermes_cli.kanban_provider_walls import live_quota_wall
    pool, _reset = _mark()
    assert not pool.has_available(model=MODEL)
    assert quota_unavailable(pool, MODEL) is None
    assert live_quota_wall('anthropic', MODEL) is None


def test_auth_churned_start_books_a_retry_without_any_reset(pool_home):
    from agent.model_routing_unavailable import SubscriptionAuthUnavailable, SubscriptionQuotaUnavailable
    from hermes_cli.kanban_quota_receipt import quota_fields
    from hermes_cli.runtime_provider import resolve_runtime_with_fallback
    from hermes_cli.turn_exit import credential_failure_flags, turn_exit_code
    _mark()
    with pytest.raises(SubscriptionAuthUnavailable) as caught:
        resolve_runtime_with_fallback(pool_home, requested='anthropic', target_model=MODEL,
                                      routing_session_id='auth-churn-start')
    assert not isinstance(caught.value, SubscriptionQuotaUnavailable)
    assert 'resets at' not in str(caught.value)
    # Transient for the worker (no failure budget), but no deadline anyone could hold on.
    assert turn_exit_code(None, kanban_worker=True, **credential_failure_flags(caught.value)) == 75
    assert quota_fields(caught.value) == {'failure_reason': 'quota_unconfirmed', 'failure_retryable': True}


def test_control_every_row_429_stays_a_typed_quota_wall(pool_home):
    from agent.model_routing_unavailable import quota_unavailable
    pool, reset = _mark(revoked=[], capped=AUTH + CAPPED)
    wall = quota_unavailable(pool, MODEL)
    assert wall is not None and wall.resets_at == pytest.approx(reset, abs=0.01)


def test_401_row_with_its_own_model_cap_still_counts_as_quota(pool_home):
    from agent.credential_pool import load_pool
    from agent.model_routing_unavailable import quota_unavailable
    pool, reset = _mark(revoked=[], capped=AUTH + CAPPED)
    for name in AUTH:  # capped first, then its token was revoked: re-auth would not lift the cap
        pool.mark_exhausted_and_rotate(status_code=401, model=MODEL, credential_id=name,
                                       error_context={'message': 'OAuth access token has been revoked'})
    wall = quota_unavailable(load_pool('anthropic', allowed_auth_types={'oauth'}), MODEL)
    assert wall is not None and wall.resets_at == pytest.approx(reset, abs=0.01)
