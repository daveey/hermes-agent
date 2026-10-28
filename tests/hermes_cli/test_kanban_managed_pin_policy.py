"""Kanban pins must pass the managed subscription policy at create time, not at worker startup. [daveey]

On the hub (``model_routing.enforce_subscription_only: true``) two cards pinned to the retired
``claude-sonnet-4-5`` were accepted by ``kanban_create``; each worker then died at startup with
"Managed subscription policy rejects unregistered or ambiguous concrete pin", exited 1 like an
ordinary crash, and was retried until triage. The create-time check only caught vendor/provider
family mismatches. These tests pin the three layers of the fix: create refuses, dispatch blocks a
pin already in the DB without spawning, and a worker that still hits the policy exits terminal.
"""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

def _managed_yaml(enabled=True):
    lines = ['model_routing:', '  enabled: true', '  policy_version: 1', '  allow_metered: false',
             f'  enforce_subscription_only: {"true" if enabled else "false"}', '  routes:']
    for level, provider, model in (('deep', 'anthropic', 'claude-opus-5-5'),
                                   ('standard', 'anthropic', 'claude-sonnet-5-5'),
                                   ('standard', 'openai-codex', 'gpt-6.1-sol')):
        if f'    {level}:' not in lines:
            lines.append(f'    {level}:')
        lines += [f'      - provider: {provider}', f'        model: {model}', '        billing: subscription',
                  '        weight: 1', '        eligible: true', '        funding_verified: true',
                  '        capabilities: {tools: true, streaming: true}']
    return '\n'.join(lines) + '\n'


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    (home / 'config.yaml').write_text('kanban:\n  orchestrator_profile: supervisor\n')

    def configure(enforce=True):
        for name in ('worker', 'supervisor'):
            profile = home / 'profiles' / name
            profile.mkdir(parents=True, exist_ok=True)
            (profile / 'config.yaml').write_text(
                'model:\n  default: claude-sonnet-5-5\n  provider: anthropic\n'
                'platform_toolsets:\n  cli: [terminal, file, kanban]\nterminal:\n  backend: local\n'
                + _managed_yaml(enforce))

    configure()
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        yield conn, configure


def _count(conn):
    return conn.execute('SELECT count(*) FROM tasks').fetchone()[0]


@pytest.mark.parametrize('provider', [None, 'anthropic'])
def test_create_refuses_retired_pin_under_managed_policy(board, provider):
    conn, _ = board
    with pytest.raises(ValueError, match='model_pin_not_in_routing_registry') as info:
        kb.create_task(conn, title='Check live', assignee='worker',
                       model_override='claude-sonnet-4-5', provider_override=provider)
    message = str(info.value)
    assert 'claude-sonnet-5-5' in message and 'claude-opus-5-5' in message
    assert 'leave model out' in message
    assert _count(conn) == 0


def test_create_accepts_registered_pin(board):
    conn, _ = board
    tid = kb.create_task(conn, title='Review', assignee='worker',
                         model_override='claude-sonnet-5-5', provider_override='anthropic')
    task = kb.get_task(conn, tid)
    assert (task.model_override, task.provider_override) == ('claude-sonnet-5-5', 'anthropic')


def test_create_refuses_registered_model_on_unregistered_provider(board):
    conn, _ = board
    with pytest.raises(ValueError, match='model_pin_not_in_routing_registry'):
        kb.create_task(conn, title='Review', assignee='worker',
                       model_override='claude-sonnet-5-5', provider_override='openrouter')


def test_class_selector_and_unpinned_cards_are_untouched(board):
    conn, _ = board
    kb.create_task(conn, title='Unpinned', assignee='worker')
    kb.create_task(conn, title='Class', assignee='worker', model_override='class:deep')
    assert _count(conn) == 2


def test_policy_off_keeps_old_behaviour(board):
    conn, configure = board
    configure(enforce=False)
    tid = kb.create_task(conn, title='Legacy', assignee='worker',
                         model_override='claude-sonnet-4-5', provider_override='anthropic')
    assert kb.get_task(conn, tid).model_override == 'claude-sonnet-4-5'


def test_set_model_override_refuses_retired_pin(board):
    conn, _ = board
    tid = kb.create_task(conn, title='Card', assignee='worker')
    with pytest.raises(ValueError, match='model_pin_not_in_routing_registry'):
        kb.set_model_override(conn, tid, 'claude-sonnet-4-5', provider='anthropic')
    assert kb.get_task(conn, tid).model_override is None


def test_kanban_create_tool_returns_typed_refusal(board, monkeypatch):
    from tools import kanban_tools as tools
    conn, _ = board
    parent = kb.create_task(conn, title='Parent', assignee='worker')
    monkeypatch.setenv('HERMES_KANBAN_TASK', parent)
    monkeypatch.setattr(tools, '_persisted_identity', lambda: 'worker')
    result = json.loads(tools._handle_create({'title': 'Child', 'model': 'claude-sonnet-4-5'}))
    assert result['code'] == 'model_pin_not_in_routing_registry'
    assert 'claude-sonnet-5-5' in result['error']
    assert _count(conn) == 1


def test_kanban_create_tool_model_description_names_the_registry():
    from tools.kanban_tools_schemas import KANBAN_CREATE_SCHEMA
    description = KANBAN_CREATE_SCHEMA['parameters']['properties']['model']['description']
    assert "routing registry" in description and 'leave this out' in description.lower()


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_dispatch_blocks_bad_pin_already_in_db_without_spawning(board, lane):
    from hermes_cli.kanban_db_dispatch import dispatch_once, guard_hold_cap_seconds
    conn, _ = board
    tid = kb.create_task(conn, title='Legacy pin', assignee='worker', session_id='chat')
    conn.execute('UPDATE tasks SET model_override=?, status=? WHERE id=?', ('claude-sonnet-4-5', lane, tid))
    conn.commit()
    spawned = []
    dispatch_once(conn, spawn_fn=lambda *args: spawned.append(args), max_spawn=1)
    assert spawned == []
    task = kb.get_task(conn, tid)
    assert task.status == 'todo'
    assert task.block_kind == 'assignment'
    assert guard_hold_cap_seconds('model_pin_not_in_routing_registry') is None


def test_dispatch_dry_run_reports_bad_pin_without_writing(board):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn, _ = board
    tid = kb.create_task(conn, title='Legacy pin', assignee='worker')
    conn.execute('UPDATE tasks SET model_override=? WHERE id=?', ('claude-sonnet-4-5', tid))
    conn.commit()
    before = list(conn.iterdump())
    result = dispatch_once(conn, dry_run=True)
    assert (tid, 'model_pin_not_in_routing_registry') in result.respawn_guarded
    assert list(conn.iterdump()) == before


def test_worker_startup_policy_rejection_is_terminal_not_a_crash(monkeypatch, tmp_path):
    """A worker that still reaches the policy check must exit EX_CONFIG (blocked after one spawn),
    not exit 1 (retried as a transient crash until triage)."""
    from agent.model_routing import ModelRoutingError
    from agent.model_routing_managed import ManagedPinRejectedError
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    home = tmp_path / 'hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr('cli._cprint', lambda *a, **k: None, raising=False)

    def shell():
        s = CLIAgentSetupMixin.__new__(CLIAgentSetupMixin)
        s.model, s.requested_provider = 'claude-sonnet-4-5', None
        s._explicit_api_key = s._explicit_base_url = None
        s._fallback_model, s.tool_progress_mode = [], 'off'
        return s

    def _reject(**kw):
        raise ManagedPinRejectedError('claude-sonnet-4-5', None, ['claude-sonnet-5-5'])

    monkeypatch.setattr('hermes_cli.runtime_provider.resolve_runtime_provider', _reject)
    rejected = shell()
    assert rejected._ensure_runtime_credentials() is False
    assert rejected._credentials_terminal is True

    def _other(**kw):  # other routing errors (e.g. credentials for a valid pin) stay retryable
        raise ModelRoutingError('Class routing credentials unavailable; legacy fallback is prohibited')

    monkeypatch.setattr('hermes_cli.runtime_provider.resolve_runtime_provider', _other)
    other = shell()
    assert other._ensure_runtime_credentials() is False
    assert other._credentials_terminal is False


def test_pin_policy_rejection_is_typed_and_keeps_its_message():
    from agent.model_routing import ModelRoutingError
    from agent.model_routing_managed import ManagedPinRejectedError, pin_policy
    routes = {'deep': [('anthropic', 'claude-opus-5-5')],
              'standard': [('anthropic', 'claude-sonnet-5-5'), ('openai-codex', 'gpt-6.1-sol')]}
    config = {'model_routing': {
        'enabled': True, 'policy_version': 1, 'allow_metered': False, 'enforce_subscription_only': True,
        'routes': {level: [{'provider': p, 'model': m, 'billing': 'subscription', 'weight': 1, 'eligible': True,
                            'funding_verified': True, 'capabilities': {'tools': True, 'streaming': True}}
                           for p, m in pairs] for level, pairs in routes.items()}}}
    with pytest.raises(ManagedPinRejectedError) as info:
        pin_policy('claude-sonnet-4-5', None, config)
    assert isinstance(info.value, ModelRoutingError)
    assert 'Managed subscription policy rejects unregistered or ambiguous concrete pin' in str(info.value)
    assert info.value.allowed == ['claude-opus-5-5', 'claude-sonnet-5-5', 'gpt-6.1-sol']
