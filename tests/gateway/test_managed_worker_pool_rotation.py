"""Managed inference must keep the resolver's pool, not only its first bearer."""
import pytest

from agent.credential_pool import CredentialPool, PooledCredential
from tests.gateway.test_managed_worker_route_key import _constructed_agent, _routed_frame


@pytest.mark.parametrize('provider,model,url,mode', [
    ('openai-codex', 'gpt-6-astra', 'https://chatgpt.com/backend-api/codex', 'codex_responses'),
    ('anthropic', 'claude-opus-5-5', 'https://api.anthropic.com', 'anthropic_messages'),
])
def test_managed_agent_keeps_resolved_pool(tmp_path, monkeypatch, provider, model, url, mode):
    import time
    entries = [PooledCredential(provider, name, name, 'oauth', i, 'manual:hub_minted',
                                'fake-' + name + '-token', expires_at_ms=int((time.time() + 3600) * 1000))
               for i, name in enumerate(('first', 'second'))]
    pool = CredentialPool(provider, entries)
    monkeypatch.setattr(pool, '_persist', lambda **kw: None)
    monkeypatch.setattr('agent.codex_entitlements.codex_entry_entitled_for_model', lambda *a: True)
    frame = _routed_frame(tmp_path, monkeypatch, model, {'provider': provider})
    monkeypatch.setattr('gateway.session_worker_construct.route_runtime', lambda *a, **k: ({
        'provider': provider, 'api_key': 'fake-first-token', 'base_url': url,
        'api_mode': mode, 'credential_pool': pool,
    }, None))
    agent = _constructed_agent(frame, monkeypatch)
    assert agent._credential_pool is pool
    from agent.agent_runtime_helpers import recover_with_credential_pool
    recovered, _ = recover_with_credential_pool(agent, status_code=429, has_retried_429=True,
        error_context={'code': 'usage_limit_reached', 'message': 'The usage limit has been reached',
                       'reset_at': time.time() + 3600})
    assert recovered
    assert agent.api_key == 'fake-second-token'
    first = next(e for e in pool.entries() if e.id == 'first')
    assert first.last_status == 'exhausted' or first.model_cooldowns.get(model, 0) > time.time()


@pytest.mark.parametrize('explicit', [False, True])
def test_worker_does_not_invent_pool_for_nonpool_route(monkeypatch, explicit):
    from types import SimpleNamespace
    from gateway.session_worker_construct import worker_route
    policy = SimpleNamespace(effective=lambda: ({'model': {}}, 'test'))
    monkeypatch.setattr('gateway.session_worker_construct.route_runtime', lambda *a, **k: ({
        'provider': 'custom:test', 'api_key': 'fake-explicit', 'base_url': 'https://example.test/v1',
    }, None))
    assert worker_route(policy, {'api_key': 'fake-explicit' if explicit else None}).get('credential_pool') is None


def test_bound_xai_launch_key_does_not_attach_profile_pool(tmp_path, monkeypatch):
    import time
    from hermes_cli import runtime_provider
    entry = PooledCredential('xai-oauth', 'profile', 'profile', 'oauth', 0, 'manual:test',
                             'fake-profile-token', expires_at_ms=int((time.time() + 3600) * 1000))
    pool = CredentialPool('xai-oauth', [entry])
    monkeypatch.setattr(pool, '_persist', lambda **kw: None)
    monkeypatch.setattr(runtime_provider, 'load_pool', lambda *a, **kw: pool)
    frame = _routed_frame(tmp_path, monkeypatch, 'grok-4', {'provider': 'xai-oauth'},
                          api_key='fake-launch-token')
    built = _constructed_agent(frame, monkeypatch)
    assert built.api_key == 'fake-launch-token'
    assert built._credential_pool is None


def test_fallback_pool_never_attaches_to_primary(monkeypatch):
    from types import SimpleNamespace
    from gateway.session_worker_construct import worker_route
    policy = SimpleNamespace(effective=lambda: ({'model': {}}, 'test'))
    monkeypatch.setattr('gateway.session_worker_construct.route_runtime', lambda *a, **k: ({
        'credential_pool': object(), 'api_key': 'fake-fallback',
    }, {'provider': 'other'}))
    assert worker_route(policy, {'api_key': None}) == {}
