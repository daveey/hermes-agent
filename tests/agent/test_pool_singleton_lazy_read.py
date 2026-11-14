"""Singleton dispatch must not load auth.json when the selected seeder does not use it."""
import pytest

from agent import credential_pool as cp


@pytest.mark.parametrize('provider,seeder', [
    ('openrouter', '_seed_profile_singleton'),
    ('anthropic', '_seed_anthropic_singletons'),
    ('qwen-oauth', '_seed_qwen_singleton'),
    ('minimax-oauth', '_seed_minimax_singleton'),
])
def test_dispatch_does_not_read_unused_auth(monkeypatch, provider, seeder):
    calls = []
    monkeypatch.setattr(cp, seeder, lambda seed: calls.append(seed.provider))
    def unexpected():
        pytest.fail('dispatch read an auth store its seeder does not consume')
    monkeypatch.setattr(cp, '_load_auth_store', unexpected)
    assert cp._seed_from_singletons(provider, []) == (False, set())
    assert calls == [provider]


@pytest.mark.parametrize('provider,seeder', [('nous', '_seed_nous_singleton'), ('openai-codex', '_seed_tokens_singleton')])
def test_dispatch_reads_current_auth_for_consumers(monkeypatch, provider, seeder):
    state = {'generation': 1}
    seen = []
    monkeypatch.setattr(cp, '_is_source_suppressed_fn', lambda: lambda *args: False)
    monkeypatch.setattr(cp, '_load_auth_store', lambda: dict(state))
    monkeypatch.setattr(cp, seeder, lambda seed, store: seen.append(store))
    cp._seed_from_singletons(provider, [])
    state['generation'] = 2
    cp._seed_from_singletons(provider, [])
    assert seen == [{'generation': 1}, {'generation': 2}]
