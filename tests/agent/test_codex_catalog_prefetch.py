"""Cold agent boot overlaps the *same* authenticated catalog request, never stale context."""
import concurrent.futures
import threading
from types import SimpleNamespace


def test_prefetch_and_context_resolution_share_one_fresh_request(monkeypatch):
    from agent import model_metadata as mm
    from agent.model_metadata_codex_prefetch import prefetch

    entered, release = threading.Event(), threading.Event()
    calls = []
    token = 'fake-codex-prefetch-test'
    monkeypatch.setattr(mm, '_codex_catalog_probe_allowed', lambda *_: True)
    monkeypatch.setattr(mm, '_codex_oauth_context_cache', {})
    monkeypatch.setattr(mm, '_codex_oauth_max_context_cache', {})
    monkeypatch.setattr('agent.codex_headers.codex_account_headers', lambda _: {})

    def get(url, **kwargs):
        calls.append((url, kwargs['headers']['Authorization']))
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(status_code=200, json=lambda: {'models': [
            {'slug': 'prefetch-model', 'context_window': 123456, 'max_context_window': 234567}]})

    monkeypatch.setattr(mm.model_metadata_http, 'get', get)
    future = prefetch(token, 'https://chatgpt.com/backend-api/codex')
    assert entered.wait(5)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            resolution = executor.submit(mm._fetch_codex_oauth_context_lengths_with_source,
                                         token, 'https://chatgpt.com/backend-api/codex')
            release.set()
            assert resolution.result(5)[0] == {'prefetch-model': 123456}
        assert future.result(5)[0] == {'prefetch-model': 123456}
        assert len(calls) == 1
        assert calls[0][1] == 'Bearer ' + token
        key = mm._codex_oauth_token_fingerprint(token, 'https://chatgpt.com/backend-api/codex')
        assert mm._codex_oauth_max_context_cache[key] == {'prefetch-model': 234567}
    finally:
        release.set()
        future.result(5)


def test_agent_prefetch_uses_final_route_before_tools(monkeypatch):
    from agent.model_metadata_codex_prefetch import prefetch_agent

    agent = SimpleNamespace(provider='openai-codex', api_key='selected-token',
                            base_url='https://chatgpt.com/backend-api/codex')
    calls = []
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', dict)
    monkeypatch.setattr('agent.model_metadata_codex_prefetch.prefetch',
                        lambda token, base: calls.append((token, base)))
    prefetch_agent(agent)
    assert calls == [(agent.api_key, agent.base_url)]
    # Keep source-order coverage behavioral: the real constructor must reach tool
    # discovery while its selected account's HTTP request is still in flight.


def test_real_constructor_overlaps_catalog_with_tools(monkeypatch):
    from unittest.mock import patch
    from agent import model_metadata as mm
    from run_agent import AIAgent

    entered, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(mm, '_codex_catalog_probe_allowed', lambda *_: True)
    monkeypatch.setattr(mm, '_codex_oauth_context_cache', {})
    monkeypatch.setattr(mm, '_codex_oauth_max_context_cache', {})
    monkeypatch.setattr('agent.codex_headers.codex_account_headers', lambda _: {})

    def get(url, **kwargs):
        calls.append(kwargs['headers']['Authorization'])
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(status_code=200, json=lambda: {'models': [
            {'slug': 'gpt-5.5', 'context_window': 123456, 'max_context_window': 234567}]})

    overlap = []
    def tools(*args, **kwargs):
        try:
            overlap.append(entered.wait(5))
        finally:
            release.set()
        return []

    monkeypatch.setattr(mm.model_metadata_http, 'get', get)
    with (patch('hermes_cli.config.load_config', return_value={}),
          patch('hermes_cli.config.load_config_readonly', return_value={}),
          patch('model_tools.get_tool_definitions', side_effect=tools),
          patch('model_tools.check_toolset_requirements', return_value={}),
          patch('agent.process_bootstrap.OpenAI')):
        try:
            agent = AIAgent(model='gpt-5.5', provider='openai-codex', api_key='selected-account',
                            base_url='https://chatgpt.com/backend-api/codex',
                            quiet_mode=True, skip_context_files=True, skip_memory=True)
            assert agent.context_compressor.context_length == 123456
            assert agent.api_key == 'selected-account'
            assert overlap == [True], 'tools did not overlap the live catalog request'
            assert calls == ['Bearer selected-account']
        finally:
            release.set()


def test_pinned_non_codex_and_ambiguous_config_do_not_prefetch(monkeypatch):
    from agent.model_metadata_codex_prefetch import prefetch_agent
    calls = []
    monkeypatch.setattr('agent.model_metadata_codex_prefetch.prefetch', lambda *args: calls.append(args))
    for provider, key, config in [
        ('anthropic', 'account', {}), ('openai-codex', '', {}),
        ('openai-codex', 'account', {'model': {'context_length': 123456}}),
        ('openai-codex', 'account', {'model': 'legacy-model'}),
        ('openai-codex', 'account', {'custom_providers': [{'name': 'private'}]}),
    ]:
        monkeypatch.setattr('hermes_cli.config.load_config_readonly', lambda: config)
        assert prefetch_agent(SimpleNamespace(provider=provider, api_key=key, base_url='https://example.test')) is None
    assert calls == []


def test_singleflight_is_scoped_to_token_and_endpoint_and_recovers_after_error():
    import pytest
    from agent.model_metadata_codex_prefetch import resolve, _inflight

    calls = []
    def fetch(token, base):
        calls.append((token, base))
        return {token + base: 100}, True
    assert resolve('a', 'https://one', fetch) == ({'ahttps://one': 100}, True)
    assert resolve('b', 'https://one', fetch) == ({'bhttps://one': 100}, True)
    assert resolve('a', 'https://two', fetch) == ({'ahttps://two': 100}, True)
    # Completed requests are not an additional/stale cache.
    assert resolve('a', 'https://one', fetch) == ({'ahttps://one': 100}, True)
    assert len(calls) == 4
    def broken(*args):
        raise OSError('fixture outage')
    with pytest.raises(OSError, match='fixture outage'):
        resolve('a', 'https://one', broken)
    assert not _inflight
    assert resolve('a', 'https://one', fetch) == ({'ahttps://one': 100}, True)


def test_prefetch_copies_profile_context_and_retains_failure(monkeypatch):
    import contextvars
    import pytest
    from agent import model_metadata as mm
    from agent.model_metadata_codex_prefetch import prefetch
    scope = contextvars.ContextVar('catalog-test-scope')
    token = scope.set(('secondary-profile', 'selected-credential'))
    observed = []
    def fetch(*args):
        observed.append(scope.get())
        raise OSError('fixture outage')
    monkeypatch.setattr(mm, '_fetch_codex_oauth_context_lengths_with_source', fetch)
    try:
        with pytest.raises(OSError, match='fixture outage'):
            prefetch('selected', 'https://one').result(5)
        assert observed == [('secondary-profile', 'selected-credential')]
    finally:
        scope.reset(token)


def test_optional_prefetch_start_failure_falls_back_but_drain_refusal_propagates(monkeypatch):
    import pytest
    from agent.model_metadata_codex_prefetch import prefetch_agent
    from hermes_cli.worker_drain_fence import DrainRefused
    agent = SimpleNamespace(provider='openai-codex', api_key='selected-token',
                            base_url='https://chatgpt.com/backend-api/codex')
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', dict)
    for error in (RuntimeError('cannot start thread'), OSError('resource unavailable')):
        def unavailable(*args, **kwargs):
            raise error
        monkeypatch.setattr('agent.model_metadata_codex_prefetch.spawn_context_thread', unavailable)
        assert prefetch_agent(agent) is None
    monkeypatch.setattr('agent.model_metadata_codex_prefetch.spawn_context_thread',
                        lambda *args, **kwargs: (_ for _ in ()).throw(DrainRefused('draining')))
    with pytest.raises(DrainRefused):
        prefetch_agent(agent)
