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
    from run_agent import AIAgent
    _legacy_row(home, LEGACY_ECHO)
    _wall(home, 'openai-codex', ASTRA, 33)
    policy = _cli_policy(home)
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
    _legacy_row(home, dict(LEGACY_ECHO, reason='explicit-switch'))
    _wall(home, 'openai-codex', ASTRA, 33)
    policy = _cli_policy(home)
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


# Every Desktop GUI chat freezes its provider's own endpoint into the creation request (MacBook chat
# 20261025_104956_99e2e1, 11-13): the policy is built by the real creation code, never faked as '{}'.
GUI_CODEX_REQUEST = {'base_url': 'https://chatgpt.com/backend-api/codex', 'provider': 'openai-codex',
                     'model': ASTRA, 'source': 'gui'}


def _cli_policy(home):
    """A CLI session's policy (no endpoint in its request), built by the real creation code."""
    from gateway.session_policy import build_policy
    from hermes_cli.config import load_config
    return build_policy({'model': ASTRA, 'provider': 'openai-codex', 'source': 'cli', 'cwd': str(home)},
                        load_config(), private_secrets={})


def _gui_policy(home, **overrides):
    from gateway.session_policy import build_policy
    from hermes_cli.config import load_config
    return build_policy(dict(GUI_CODEX_REQUEST, cwd=str(home), **overrides), load_config(), private_secrets={})


def _gateway_turn(monkeypatch, policy):
    from agent.model_routing_runtime import routing_session
    from gateway.run_turn_prepare import _resolve_policy_agent_runtime
    runner = SimpleNamespace(_pre_agent_fallback_notice=None)
    monkeypatch.setattr('gateway.session_authorities.active_authority', lambda runner: None)
    with routing_session(SID):
        return runner, _resolve_policy_agent_runtime(runner, policy)


def _not_deferred(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith('routing: walled pin not deferred')]


def test_gateway_gui_chat_on_its_own_codex_url_builds_on_opus(home, monkeypatch, caplog):
    """The chat's saved native Codex URL overrides nothing: the walled default pin still reroutes."""
    from run_agent import AIAgent
    _legacy_row(home, LEGACY_ECHO)
    _wall(home, 'openai-codex', ASTRA, 110)
    policy = _gui_policy(home)
    assert json.loads(policy.request_json)['base_url'] == GUI_CODEX_REQUEST['base_url']
    with caplog.at_level(logging.WARNING):
        runner, (model, kwargs) = _gateway_turn(monkeypatch, policy)
    assert model == ASTRA and kwargs['provider'] == 'auto' and not kwargs['api_key'], kwargs
    notice = runner._pre_agent_fallback_notice
    assert notice and 'quota is out until' in notice and OPUS in notice, notice
    assert not _not_deferred(caplog)
    agent = AIAgent(model=model, session_id=SID, quiet_mode=True, skip_memory=True, skip_context_files=True,
                    **{k: v for k, v in kwargs.items() if k in ('provider', 'api_key', 'base_url', 'api_mode')})
    assert (agent.model, agent.provider) == (OPUS, 'anthropic')


def test_gateway_gui_chat_explicit_switch_pin_still_refuses_and_says_why(home, monkeypatch, caplog):
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    _legacy_row(home, dict(LEGACY_ECHO, reason='explicit-switch'))
    _wall(home, 'openai-codex', ASTRA, 110)
    policy = _gui_policy(home)
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            with pytest.raises(SubscriptionQuotaUnavailable):
                _gateway_turn(monkeypatch, policy)
    skipped = _not_deferred(caplog)
    assert len(skipped) == 1 and 'reason=explicit' in skipped[0], skipped  # once per episode


def test_gateway_gui_chat_on_a_foreign_endpoint_still_refuses(home, monkeypatch):
    from agent.model_routing import ModelRoutingError
    _legacy_row(home, LEGACY_ECHO)
    _wall(home, 'openai-codex', ASTRA, 110)
    policy = _gui_policy(home, base_url='https://example.com/v1')
    with pytest.raises(ModelRoutingError):
        _gateway_turn(monkeypatch, policy)


def _injected_wall_refusal(home, monkeypatch, caplog, *, key=None, hours=110, session=True, **policy_overrides):
    """route_runtime with the inner resolver raising the wall directly: the refusal and its log line."""
    import contextlib
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from gateway.session_worker_construct import route_runtime
    from agent.model_routing_runtime import routing_session
    _legacy_row(home, LEGACY_ECHO)
    _wall(home, 'openai-codex', ASTRA, 110)
    policy = _gui_policy(home, **policy_overrides)
    frozen, model = policy.effective(None)
    reset = None if hours is None else time.time() + hours * HOUR
    monkeypatch.setattr('gateway.session_worker_construct._route_runtime',
                        lambda *a, **k: (_ for _ in ()).throw(SubscriptionQuotaUnavailable('openai-codex', ASTRA,
                                                                                           reset)))
    scope = routing_session(SID) if session else contextlib.nullcontext()
    with caplog.at_level(logging.WARNING), scope, pytest.raises(SubscriptionQuotaUnavailable):
        route_runtime(policy, frozen, model, key)
    return _not_deferred(caplog)


def test_walled_pin_with_a_launch_key_logs_why_it_was_not_deferred(home, monkeypatch, caplog):
    skipped = _injected_wall_refusal(home, monkeypatch, caplog, key='***')
    assert len(skipped) == 1 and 'reason=key' in skipped[0], skipped


def test_walled_pin_on_a_foreign_endpoint_refuses_before_deferral_and_says_why(home, monkeypatch, caplog):
    """The managed resolver normally refuses a foreign URL first; route_runtime keeps that refusal too."""
    skipped = _injected_wall_refusal(home, monkeypatch, caplog, base_url='https://example.com/v1')
    assert len(skipped) == 1 and 'reason=endpoint' in skipped[0], skipped


def test_walled_pin_outside_a_routing_session_logs_no_session(home, monkeypatch, caplog):
    skipped = _injected_wall_refusal(home, monkeypatch, caplog, session=False)
    assert len(skipped) == 1 and 'reason=no-session' in skipped[0], skipped


def test_walled_pin_with_nothing_else_in_its_class_logs_no_target(home, monkeypatch, caplog):
    monkeypatch.setattr('agent.model_routing_walls.predicted_target', lambda *a, **k: [])
    skipped = _injected_wall_refusal(home, monkeypatch, caplog)
    assert len(skipped) == 1 and 'reason=no-target' in skipped[0], skipped


@pytest.mark.parametrize('hours', [1, None], ids=['short-wall', 'unknown-reset'])
def test_a_short_or_unknown_wall_refuses_quietly(home, monkeypatch, caplog, hours):
    assert _injected_wall_refusal(home, monkeypatch, caplog, key='***', hours=hours) == []


# --- Desktop/TUI gateway (tui_gateway.agent_factory): the same reroute, on its own agent-build path -------------
# Hub Desktop chats 20261031_222913_405ade and 20261031_064851_d0f106 (11-13): router-picked Astra pins, Codex
# walled until 11-17, 187 failed builds, because only the messaging gateway's pre-build lookup deferred to the
# agent build's reroute.

DESKTOP_ROW = {'model': ASTRA, 'provider': 'openai-codex'}  # what a resumed Desktop session row carries


def _desktop_agent(model, runtime):
    from run_agent import AIAgent
    return AIAgent(model=model, session_id=SID, quiet_mode=True, skip_memory=True, skip_context_files=True,
                   **{k: runtime.get(k) for k in ('provider', 'api_key', 'base_url', 'api_mode')})


def test_desktop_session_with_legacy_astra_pin_builds_on_opus(home):
    from tui_gateway.server import _resolve_agent_model_runtime  # agent_factory binds into the server namespace
    _legacy_row(home, LEGACY_ECHO)
    _wall(home, 'openai-codex', ASTRA, 33)
    model, runtime = _resolve_agent_model_runtime(DESKTOP_ROW, None, SID)
    assert model == ASTRA and runtime['provider'] == 'auto' and not runtime['api_key'], runtime
    assert 'class_deferred' not in runtime and 'walled_notice' not in runtime, runtime
    notice = runtime.pop('_fallback_notice')  # what _make_agent pops and shows once
    assert 'quota is out until' in notice and 'PT' in notice and OPUS in notice, notice
    agent = _desktop_agent(model, runtime)
    assert (agent.model, agent.provider) == (OPUS, 'anthropic')
    assert agent.model_routing['walled_from']['model'] == ASTRA
    # The next build of this session follows the reroute without announcing it again.
    model, runtime = _resolve_agent_model_runtime(DESKTOP_ROW, None, SID)
    assert '_fallback_notice' not in runtime, runtime
    again = _desktop_agent(model, runtime)
    assert (again.model, again.provider) == (OPUS, 'anthropic')


def test_desktop_session_with_router_picked_astra_builds_on_opus(home):
    """A row written by the current allocator (``pin_source: default``), not only a legacy one."""
    from tui_gateway.server import _resolve_agent_model_runtime  # agent_factory binds into the server namespace
    _router_picks_astra(home)
    _wall(home, 'openai-codex', ASTRA, 33)
    model, runtime = _resolve_agent_model_runtime(DESKTOP_ROW, None, SID)
    runtime.pop('_fallback_notice')
    agent = _desktop_agent(model, runtime)
    assert (agent.model, agent.provider) == (OPUS, 'anthropic')


def test_desktop_row_with_its_providers_native_endpoint_still_moves_off_the_wall(home):
    """A resumed row keeps the base_url/api_mode its last turn ran on. Codex's own endpoint and wire override
    nothing, so the Desktop build defers like the messaging gateway (``overrides_endpoint``) and lands on Opus;
    the row's stale codex wire must not ride along onto the rerouted runtime."""
    from hermes_cli.auth_constants import DEFAULT_CODEX_BASE_URL
    from tui_gateway.server import _resolve_agent_model_runtime  # agent_factory binds into the server namespace
    _legacy_row(home, LEGACY_ECHO)
    _wall(home, 'openai-codex', ASTRA, 33)
    row = dict(DESKTOP_ROW, base_url=DEFAULT_CODEX_BASE_URL, api_mode='codex_responses')
    model, runtime = _resolve_agent_model_runtime(row, None, SID)
    assert 'quota is out until' in runtime.pop('_fallback_notice'), runtime
    assert runtime.get('base_url') != DEFAULT_CODEX_BASE_URL and runtime.get('api_mode') != 'codex_responses', runtime
    agent = _desktop_agent(model, runtime)
    assert (agent.model, agent.provider) == (OPUS, 'anthropic')
    assert agent.model_routing['walled_from']['model'] == ASTRA


@pytest.mark.parametrize('case', ['explicit switch', 'short wall', 'no session id', 'own endpoint', 'own key'])
def test_desktop_wall_still_refuses_when_the_pin_must_not_move(home, case):
    """Controls: David's own switch, a wall under the reroute threshold, a build with no session identity
    (the readiness probe) and a session that overrides its endpoint or key all keep the refusal."""
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from hermes_cli.auth_constants import DEFAULT_CODEX_BASE_URL
    from tui_gateway.server import _resolve_runtime_or_defer_wall
    _legacy_row(home, dict(LEGACY_ECHO, reason='explicit-switch') if case == 'explicit switch' else LEGACY_ECHO)
    _wall(home, 'openai-codex', ASTRA, 2 if case == 'short wall' else 33)
    overrides = {'own endpoint': {'base_url': 'https://proxy.example/v1'},
                 'own key': {'base_url': DEFAULT_CODEX_BASE_URL, 'api_key': '***'}}.get(case, {})
    session = None if case == 'no session id' else SID
    with pytest.raises(SubscriptionQuotaUnavailable):
        _resolve_runtime_or_defer_wall({'requested': 'openai-codex', 'target_model': ASTRA}, ASTRA,
                                       'openai-codex', overrides, session)


def test_desktop_wall_refuses_when_nothing_else_in_the_class_can_serve(home):
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from tui_gateway.server import _resolve_agent_model_runtime  # agent_factory binds into the server namespace
    _legacy_row(home, LEGACY_ECHO)
    _wall(home, 'openai-codex', ASTRA, 33)
    _wall(home, 'anthropic', OPUS, 33)
    with pytest.raises(SubscriptionQuotaUnavailable):
        _resolve_agent_model_runtime(DESKTOP_ROW, None, SID)


def test_make_agent_resolves_under_the_session_it_builds():
    """_make_agent is too heavy to run here; it must hand the resolver the id the route store is keyed by."""
    import inspect
    from tui_gateway import server
    source = inspect.getsource(server._make_agent)
    assert '_resolve_agent_model_runtime(model_override, provider_override, session_id or key)' in source
