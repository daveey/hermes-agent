"""Adoption must not reinterpret persisted routing provenance as a new pin."""
import json

import pytest

from gateway.session_local_migration import legacy_launch_params


@pytest.mark.parametrize('reason', ['allocation', 'explicit-switch'])
def test_legacy_launch_keeps_recorded_exact_pin(monkeypatch, reason):
    monkeypatch.setattr('agent.model_routing_restore.stale_managed_pin', lambda *args: False)
    row = {
        'id': 'legacy', 'source': 'desktop', 'cwd': '/workspace',
        'model': 'gpt-6-astra', 'billing_provider': 'openai-codex',
        'billing_base_url': 'https://chatgpt.com/backend-api/codex',
        'model_config': json.dumps({
            'provider': 'openai-codex', 'base_url': 'https://chatgpt.com/backend-api/codex',
            'api_mode': 'codex_responses', 'reasoning_config': {'effort': 'high'},
            'model_routing': {
                'exact_pin': True, 'selected_model': 'claude-opus-5-5',
                'selected_provider': 'anthropic', 'requested_class': 'deep', 'reason': reason,
            },
        }),
    }
    params, mode = legacy_launch_params(row)
    assert params == {'cwd': '/workspace', 'model': 'claude-opus-5-5',
                      'provider': 'anthropic', 'reasoning': 'high'}
    assert mode is None  # the old provider's endpoint and wire mode must not follow


def test_legacy_launch_retains_matching_route_endpoint(monkeypatch):
    monkeypatch.setattr('agent.model_routing_restore.stale_managed_pin', lambda *args: False)
    row = {'source': 'desktop', 'model': 'gpt-6-astra', 'model_config': {
        'provider': 'openai-codex', 'base_url': 'https://chatgpt.com/backend-api/codex',
        'api_mode': 'codex_responses', 'model_routing': {
            'exact_pin': True, 'selected_model': 'gpt-6-astra',
            'selected_provider': 'openai-codex', 'reason': 'explicit-switch',
        },
    }}
    params, mode = legacy_launch_params(row)
    assert params['model'] == 'gpt-6-astra'
    assert params['provider'] == 'openai-codex'
    assert params['base_url'] == 'https://chatgpt.com/backend-api/codex'
    assert mode == 'codex_responses'
