"""A router-chosen route is not David's pin: a far quota wall moves it within its class. [daveey]

MacBook Desktop chat 20261026_000514_23ef49 (11-02 13:59 PT): the router allocated openai-codex/gpt-6-astra
for the chat's ``deep`` class; the gateway then resolved that concrete model under the session id, and
``RoutingState._allocate`` upgraded the allocation to ``exact_pin``. When the Codex subscription hit a 33 h
wall, every turn failed before it started ("Subscription quota unavailable for openai-codex/gpt-6-astra")
although claude-opus-5-5 in the same class was available the whole time.

The allocation store now records ``pin_source`` (``explicit``: an honoured switch or a model David named;
``default``: a router pick, the config default, an echo). An echo never upgrades a router pick to exact,
and a non-explicit pin walled for 6 h or more is rerouted within its class (``walled_from`` recorded).
"""
import base64
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

OPUS, ASTRA = 'claude-opus-5-5', 'gpt-6-astra'
SID = '20261026_000514_23ef49'
HOUR = 3600


def _jwt(account, plan):
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b'=').decode()
    return '.'.join((seg({'alg': 'none'}), seg({'exp': int(time.time()) + 3600, 'sub': account,
                     'https://api.openai.com/auth': {'chatgpt_account_id': account, 'chatgpt_plan_type': plan}}),
                     'sig'))


def _route(provider, model):
    return {'provider': provider, 'model': model, 'billing': 'subscription', 'weight': 1, 'eligible': True,
            'funding_verified': True,
            'capabilities': {'tools': True, 'vision': True, 'streaming': True, 'context_tokens': 200000}}


def _write_auth(home):
    (home / 'auth.json').write_text(json.dumps({'version': 1, 'credential_pool': {
        'anthropic': [{'id': 'sub', 'label': 'sub', 'priority': 0, 'auth_type': 'oauth', 'source': 'manual',
                       'access_token': 'sk-ant-oat-fixture'}],
        'openai-codex': [{'id': 'pro', 'label': 'pro', 'priority': 0, 'auth_type': 'oauth',
                          'source': 'manual:device_code', 'access_token': _jwt('acct-pro', 'pro'),
                          'refresh_token': 'r'}]}}), encoding='utf-8')


def _config():
    return {'model': {'default': OPUS, 'provider': 'anthropic', 'context_length': 200000},
            'model_routing': {'enabled': True, 'policy_version': 1, 'allow_metered': False,
                              'enforce_subscription_only': True,
                              'routes': {'deep': [_route('anthropic', OPUS), _route('openai-codex', ASTRA)]}}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    from hermes_cli.config import atomic_config_write
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex'))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    atomic_config_write(tmp_path / 'config.yaml', _config())
    _write_auth(tmp_path)

    def no_network(*args, **kwargs):
        raise AssertionError(f'unexpected HTTP send: {args[1] if len(args) > 1 else args}')
    monkeypatch.setattr('httpx.Client.send', no_network)
    monkeypatch.setattr('httpx.AsyncClient.send', no_network)
    monkeypatch.setattr('agent.anthropic_adapter.build_anthropic_client', lambda *a, **k: MagicMock())
    monkeypatch.setattr('model_tools.get_tool_definitions', lambda *a, **k: [])
    return tmp_path


def _state(home):
    from agent.model_routing_state import RoutingState
    return RoutingState(home / 'model-routing.sqlite3')


def _resolve(model, provider, session=SID):
    from hermes_cli.runtime_provider import resolve_runtime_provider
    return resolve_runtime_provider(requested=provider, target_model=model, routing_session_id=session)


def _router_picks_astra(home):
    """The router's own class allocation for SID lands on Astra (the cycle's second provider)."""
    _resolve('class:deep', 'auto', session='warm-up')
    first = _resolve('class:deep', 'auto')
    assert (first['provider'], first['model']) == ('openai-codex', ASTRA)
    return first


def _legacy_row(home, metadata):
    """A row exactly as the pre-fix allocator stored it (no ``pin_source``)."""
    import sqlite3
    state = _state(home)
    with sqlite3.connect(state.path) as db:
        state._ensure_schema(db)
        db.execute('INSERT OR REPLACE INTO aliases VALUES (?, ?)', (SID, SID))
        db.execute('INSERT OR REPLACE INTO routes VALUES (?, ?, ?)', (SID, 'deep', json.dumps(metadata)))


LEGACY_ECHO = {'requested_class': 'deep', 'policy_version': 1, 'selected_provider': 'openai-codex',
               'selected_model': ASTRA, 'billing_group': 'subscription', 'reason': 'allocation',
               'exact_pin': True}


def _wall(home, provider, model, hours):
    from agent.credential_pool import load_pool
    reset = time.time() + hours * HOUR
    pool = load_pool(provider, allowed_auth_types={'oauth'})
    entry_id = pool.entries()[0].id
    pool.mark_exhausted_and_rotate(status_code=429, model=model, credential_id=entry_id,
                                   error_context={'reset_at': reset}, failure_reason='rate_limit')
    return reset


def test_echo_of_the_router_pick_stays_a_class_allocation(home):
    _router_picks_astra(home)
    echoed = _resolve(ASTRA, 'openai-codex')
    assert (echoed['provider'], echoed['model']) == ('openai-codex', ASTRA)
    recorded = _state(home).peek(SID, 'deep')
    assert not recorded.get('exact_pin'), recorded
    assert recorded['reason'] == 'allocation'
    assert recorded['pin_source'] == 'default'


def test_walled_legacy_default_pin_moves_to_anthropic_deep(home, caplog):
    _legacy_row(home, LEGACY_ECHO)
    reset = _wall(home, 'openai-codex', ASTRA, 33)
    with caplog.at_level(logging.WARNING):
        moved = _resolve(ASTRA, 'openai-codex')
        again = _resolve(ASTRA, 'openai-codex')
    assert (moved['provider'], moved['model']) == ('anthropic', OPUS)
    assert (again['provider'], again['model']) == ('anthropic', OPUS)
    recorded = _state(home).peek(SID, 'deep')
    assert recorded['reason'] == 'same-class-failover'
    assert recorded['pin_source'] == 'default'
    assert not recorded.get('exact_pin')
    assert recorded['walled_from']['provider'] == 'openai-codex'
    assert recorded['walled_from']['model'] == ASTRA
    assert recorded['walled_from']['resets_at'] == pytest.approx(reset, abs=1)
    warnings = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith('routing: walled default pin rerouted')]
    assert len(warnings) == 1, warnings  # once per episode, not once per turn
    assert f'openai-codex/{ASTRA}→anthropic/{OPUS} until ' in warnings[0]


def test_rerouted_chat_stays_on_its_reroute_after_the_wall_clears(home):
    _legacy_row(home, LEGACY_ECHO)
    _wall(home, 'openai-codex', ASTRA, 33)
    assert _resolve(ASTRA, 'openai-codex')['model'] == OPUS
    _write_auth(home)  # the Codex wall is gone; the session's stale model is still Astra
    after = _resolve(ASTRA, 'openai-codex')
    assert (after['provider'], after['model']) == ('anthropic', OPUS)


def test_walled_explicit_switch_pin_still_holds(home):
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    _legacy_row(home, dict(LEGACY_ECHO, reason='explicit-switch'))
    _wall(home, 'openai-codex', ASTRA, 33)
    with pytest.raises(SubscriptionQuotaUnavailable):
        _resolve(ASTRA, 'openai-codex')
    assert _state(home).peek(SID, 'deep')['selected_model'] == ASTRA


def test_walled_named_model_on_a_new_session_still_holds(home):
    """A first allocation of a model that is not the config default was named by David (-m, picker)."""
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    _wall(home, 'openai-codex', ASTRA, 33)
    with pytest.raises(SubscriptionQuotaUnavailable):
        _resolve(ASTRA, 'openai-codex', session='named-astra')


def test_walled_config_default_on_a_new_session_moves(home):
    _wall(home, 'anthropic', OPUS, 33)
    moved = _resolve(OPUS, 'anthropic', session='default-opus')
    assert (moved['provider'], moved['model']) == ('openai-codex', ASTRA)
    assert _state(home).peek('default-opus', 'deep')['walled_from']['model'] == OPUS


def test_wall_under_six_hours_still_holds(home):
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    _legacy_row(home, LEGACY_ECHO)
    _wall(home, 'openai-codex', ASTRA, 2)
    with pytest.raises(SubscriptionQuotaUnavailable):
        _resolve(ASTRA, 'openai-codex')
    assert _state(home).peek(SID, 'deep') == LEGACY_ECHO


def test_kanban_card_pins_are_not_moved(home, monkeypatch):
    """Card pins are owned by t_01a19990897b9d9b (David's D1): a worker's first pin stays explicit."""
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_card')
    _wall(home, 'anthropic', OPUS, 33)
    with pytest.raises(SubscriptionQuotaUnavailable):
        _resolve(OPUS, 'anthropic', session='card-opus')


def test_gateway_local_turn_with_legacy_astra_pin_builds_on_opus(home, monkeypatch):
    """The pre-build lookup (``route_runtime``) and the agent build both take the reroute."""
    from agent.model_routing_runtime import routing_session
    from gateway.run_turn_prepare import _resolve_policy_agent_runtime
    from hermes_cli.config import load_config
    from run_agent import AIAgent
    _legacy_row(home, LEGACY_ECHO)
    _wall(home, 'openai-codex', ASTRA, 33)
    frozen = dict(load_config())
    frozen['model'] = dict(frozen['model'], default=ASTRA, provider='openai-codex')
    policy = SimpleNamespace(request_json='{}', model=ASTRA,
                             effective=lambda authority=None, secrets=True: (frozen, ASTRA))
    runner = SimpleNamespace(_pre_agent_fallback_notice=None)
    monkeypatch.setattr('gateway.session_authorities.active_authority', lambda runner: None)
    monkeypatch.setattr('gateway.session_policy.route_launch_key', lambda *a: None)
    with routing_session(SID):
        model, kwargs = _resolve_policy_agent_runtime(runner, policy)
    assert model == ASTRA and kwargs['provider'] == 'auto' and not kwargs['api_key'], kwargs
    notice = runner._pre_agent_fallback_notice
    assert notice and 'quota is out until' in notice and 'PT' in notice and OPUS in notice, notice
    agent = AIAgent(model=model, session_id=SID, quiet_mode=True, skip_memory=True, skip_context_files=True,
                    **{k: v for k, v in kwargs.items() if k in ('provider', 'api_key', 'base_url', 'api_mode')})
    assert (agent.model, agent.provider) == (OPUS, 'anthropic')
    assert agent.model_routing['walled_from']['model'] == ASTRA
    # The next turn follows the reroute without announcing it again.
    runner._pre_agent_fallback_notice = None
    with routing_session(SID):
        _resolve_policy_agent_runtime(runner, policy)
    assert runner._pre_agent_fallback_notice is None


def test_gateway_local_turn_with_explicit_switch_pin_still_refuses(home, monkeypatch):
    """Control on the gateway path: David's own switch is never deferred to a reroute."""
    from agent.model_routing_runtime import routing_session
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from gateway.run_turn_prepare import _resolve_policy_agent_runtime
    from hermes_cli.config import load_config
    _legacy_row(home, dict(LEGACY_ECHO, reason='explicit-switch'))
    _wall(home, 'openai-codex', ASTRA, 33)
    frozen = dict(load_config())
    frozen['model'] = dict(frozen['model'], default=ASTRA, provider='openai-codex')
    policy = SimpleNamespace(request_json='{}', model=ASTRA,
                             effective=lambda authority=None, secrets=True: (frozen, ASTRA))
    runner = SimpleNamespace(_pre_agent_fallback_notice=None)
    monkeypatch.setattr('gateway.session_authorities.active_authority', lambda runner: None)
    monkeypatch.setattr('gateway.session_policy.route_launch_key', lambda *a: None)
    with routing_session(SID), pytest.raises(SubscriptionQuotaUnavailable):
        _resolve_policy_agent_runtime(runner, policy)
    assert runner._pre_agent_fallback_notice is None


def test_gateway_explicit_pin_reply_names_the_reset_and_model_command(home):
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from gateway.config import Platform
    from gateway.run_turn_runner import _resolution_failure_reply
    reset = time.time() + 33 * HOUR
    text, _, _ = _resolution_failure_reply(SubscriptionQuotaUnavailable('openai-codex', ASTRA, reset),
                                           Platform.LOCAL)
    assert 'PT' in text and '/model' in text and ASTRA in text, text
