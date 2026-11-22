"""Tool credentials require explicit selection; platform delivery remains owner-only."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.gateway.test_managed_turn_contract import assignment


@pytest.mark.parametrize('selection, expected', [
    ({}, {}),
    ({'platform_toolsets': {'cli': ['web']}}, {'EXA_API_KEY': 'synthetic-search'}),
    ({'platform_toolsets': {'cli': "['web']"}}, {'EXA_API_KEY': 'synthetic-search'}),
    ({'platform_toolsets': {'cli': ['hermes']}}, {'EXA_API_KEY': 'synthetic-search'}),
    ({'platform_toolsets': {'cli': ['web', 12306]}}, {'EXA_API_KEY': 'synthetic-search'}),
    ({'platform_toolsets': {'acp': ['web']}}, {}),
    ({'platform_toolsets': {'cli': []}}, {}),
    ({'platform_toolsets': {'cli': 'invalid'}}, {}),
    ({'platform_toolsets': {'cli': ['discord']}}, {}),
])
def test_worker_stages_only_explicit_tool_credentials(tmp_path, monkeypatch, selection, expected):
    from gateway import session_worker_credentials as credentials
    from gateway.session_managed_worker import _bootstrap
    from gateway.session_policy import _resolve_toolsets
    from tools.registry import registry

    _, (authority, ref, row, policy, scope) = assignment(tmp_path, monkeypatch)
    values = {'OPENAI_API_KEY': 'synthetic-route', 'EXA_API_KEY': 'synthetic-search',
              'DISCORD_BOT_TOKEN': 'synthetic-discord'}
    config = {'model': {'provider': 'custom', 'base_url': 'https://route.invalid'}, **selection}
    policy = replace(policy, source='cli', config_json=json.dumps(config), request_json='{}',
                     toolsets=tuple(_resolve_toolsets({}, config, 'cli', False)))
    monkeypatch.setattr(credentials, '_profile_values', lambda *args: values)
    monkeypatch.setattr(registry, 'get_all_entries', lambda: [
        SimpleNamespace(name='web_search', requires_env=['EXA_API_KEY']),
        SimpleNamespace(name='discord', requires_env=['DISCORD_BOT_TOKEN'])])
    _bootstrap(authority, ref, row, policy, scope)
    material = credentials.resolve_credentials(authority, scope)
    assert material['environment'] == {'OPENAI_API_KEY': 'synthetic-route', **expected}


@pytest.mark.parametrize('name', [
    'DISCORD_BOT_TOKEN', 'SLACK_TOKEN', 'SLACK_SIGNING_SECRET', 'TELEGRAM_TOKEN',
    'OTHER_BOT_TOKEN', 'OTHER_APP_TOKEN', 'other_bot_token', 'slack_token', 'HERMES_MODEL_API_KEY',
])
def test_explicit_tools_cannot_stage_platform_or_internal_secrets(tmp_path, monkeypatch, name):
    from gateway import session_worker_credentials as credentials
    from tools.registry import registry
    import toolsets

    policy = SimpleNamespace(request_json='{"toolsets": ["fixture"]}', source='cli',
                             current_toolsets=lambda config: ('fixture',))
    monkeypatch.setattr(credentials, '_profile_values', lambda *args: {name: 'synthetic-owner-only'})
    monkeypatch.setattr(toolsets, 'resolve_toolset', lambda toolset: ['fixture'])
    monkeypatch.setattr(registry, 'get_all_entries', lambda: [
        SimpleNamespace(name='fixture', requires_env=[name])])
    assert credentials.tool_environment(SimpleNamespace(profile_id=str(tmp_path)), policy, {}) == {}


@pytest.mark.parametrize('source, launch, config, expected', [
    ('acp', {}, {'platform_toolsets': {'acp': ['web']}}, True),
    ('acp', {}, {'platform_toolsets': {'cli': ['web']}}, False),
    ('gui', {}, {'platform_toolsets': {'cli': ['web']}}, True),
    ('cli', {'toolsets': ['web']}, {}, True),
    ('cli', {'toolsets': []}, {'platform_toolsets': {'cli': ['web']}}, False),
    ('cli', {'toolsets': ['other']}, {'platform_toolsets': {'cli': ['web']}}, False),
])
def test_explicit_request_precedence_and_surface_resolution(tmp_path, monkeypatch, source, launch, config, expected):
    from gateway import session_worker_credentials as credentials
    from tools.registry import registry
    import toolsets

    policy = SimpleNamespace(request_json=json.dumps(launch), source=source,
                             current_toolsets=lambda config: ('web',))
    monkeypatch.setattr(credentials, '_profile_values', lambda *args: {'SEARCH_KEY': 'synthetic-search'})
    monkeypatch.setattr(toolsets, 'resolve_toolset', lambda toolset: [toolset])
    monkeypatch.setattr(registry, 'get_all_entries', lambda: [
        SimpleNamespace(name='web', requires_env=['SEARCH_KEY'])])
    assert credentials.tool_environment(SimpleNamespace(profile_id=str(tmp_path)), policy, config) == (
        {'SEARCH_KEY': 'synthetic-search'} if expected else {})


def test_explicit_selection_does_not_grant_implicitly_added_or_disabled_tools(tmp_path, monkeypatch):
    from gateway import session_worker_credentials as credentials
    from tools.registry import registry
    import toolsets

    policy = SimpleNamespace(request_json='{"toolsets": ["web", "disabled"]}', source='cli',
                             current_toolsets=lambda config: ('web', 'implicit'))
    monkeypatch.setattr(credentials, '_profile_values', lambda *args: {
        'SEARCH_KEY': 'synthetic-search', 'IMPLICIT_KEY': 'synthetic-implicit',
        'DISABLED_KEY': 'synthetic-disabled'})
    monkeypatch.setattr(toolsets, 'resolve_toolset', lambda toolset: [toolset])
    monkeypatch.setattr(registry, 'get_all_entries', lambda: [
        SimpleNamespace(name=tool, requires_env=[key]) for tool, key in
        [('web', 'SEARCH_KEY'), ('implicit', 'IMPLICIT_KEY'), ('disabled', 'DISABLED_KEY')]])
    assert credentials.tool_environment(SimpleNamespace(profile_id=str(tmp_path)), policy, {}) == {
        'SEARCH_KEY': 'synthetic-search'}
