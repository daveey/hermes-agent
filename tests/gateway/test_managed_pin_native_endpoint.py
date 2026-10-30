"""[daveey] A managed pin frozen with its provider's own endpoint still runs.

``adopt_legacy_session`` freezes a Desktop chat's saved endpoint into the launch request, and
``route_runtime`` hands that URL to the resolver as ``explicit_base_url``. The managed guard refused
every explicit URL, so all 14 adopted chats failed each turn with "Managed subscription pins cannot
override credentials or endpoint", and the Desktop card called it a rejected sign-in.
"""
import pytest

ANTHROPIC = ('anthropic', 'claude-opus-5-5', 'https://api.anthropic.com')
CODEX = ('openai-codex', 'gpt-6-astra', 'https://chatgpt.com/backend-api/codex')


def _route(provider, model):
    return {'provider': provider, 'model': model, 'billing': 'subscription', 'weight': 1,
            'capabilities': {}}


CONFIG = {'model': {'default': ANTHROPIC[1], 'provider': ANTHROPIC[0]},
          'model_routing': {'enabled': True, 'policy_version': 1, 'allow_metered': False,
                            'enforce_subscription_only': True,
                            'routes': {'deep': [_route(*ANTHROPIC[:2]), _route(*CODEX[:2])]}}}


@pytest.fixture
def managed(tmp_path, monkeypatch):
    """The real guard over a stubbed allocation: every call that passes it lands in ``calls``."""
    from hermes_cli.config import atomic_config_write
    home = tmp_path / 'home'
    home.mkdir()
    atomic_config_write(home / 'config.yaml', CONFIG)
    monkeypatch.setenv('HERMES_HOME', str(home))
    calls = []

    def allocate(model, provider, **kwargs):
        calls.append((model, provider))
        return {'provider': provider, 'requested_provider': provider, 'api_key': 'pool', 'base_url': 'native',
                'api_mode': None}

    monkeypatch.setattr('agent.model_routing_managed.resolve_managed_runtime', allocate)
    return calls


def _adopted_policy(tmp_path, provider, model, base_url):
    from gateway.session_policy import build_policy
    config = {**CONFIG, 'model': {'default': model, 'provider': provider, 'base_url': base_url}}
    return build_policy({'source': 'gui', 'cwd': str(tmp_path), 'toolsets': [], 'model': model,
                         'provider': provider, 'base_url': base_url}, config, profile_terminal=False)


@pytest.mark.parametrize('route', [ANTHROPIC, CODEX], ids=['anthropic', 'codex'])
def test_resolver_serves_a_pin_on_its_own_endpoint(managed, route):
    from hermes_cli.runtime_provider import resolve_runtime_provider
    provider, model, base_url = route
    runtime = resolve_runtime_provider(requested=provider, target_model=model, explicit_base_url=base_url)
    assert managed == [(model, provider)]
    assert runtime['api_key'] == 'pool'


@pytest.mark.parametrize('route', [ANTHROPIC, CODEX], ids=['anthropic', 'codex'])
@pytest.mark.parametrize('fallback', [True, False])
def test_an_adopted_chat_route_runs_on_its_frozen_native_endpoint(managed, tmp_path, route, fallback):
    from gateway.session_worker_construct import route_runtime
    provider, model, base_url = route
    policy = _adopted_policy(tmp_path, provider, model, base_url)
    frozen, effective = policy.effective()
    runtime, fallback_entry = route_runtime(policy, frozen, effective, None, fallback=fallback)
    assert fallback_entry is None
    assert managed == [(model, provider)] and runtime['provider'] == provider


@pytest.mark.parametrize('provider,base_url', [
    ('anthropic', 'https://proxy.example.com/v1'),
    ('anthropic', 'https://openrouter.ai/api/v1'),
    ('openai-codex', 'https://api.anthropic.com'),
    ('auto', 'https://api.anthropic.com'),
    (None, 'https://api.anthropic.com'),
    ('custom', 'https://api.anthropic.com'),
], ids=['unknown-host', 'other-vendor', 'crossed-native', 'auto', 'no-provider', 'custom'])
def test_a_real_endpoint_override_is_still_refused(managed, provider, base_url):
    from agent.model_routing import ModelRoutingError
    from hermes_cli.runtime_provider import resolve_runtime_provider
    with pytest.raises(ModelRoutingError, match='cannot override'):
        resolve_runtime_provider(requested=provider, target_model=ANTHROPIC[1], explicit_base_url=base_url)
    assert managed == []


@pytest.mark.parametrize('base_url', [None, ANTHROPIC[2]], ids=['key-alone', 'key-with-native-url'])
def test_an_explicit_key_is_still_refused(managed, base_url):
    from agent.model_routing import ModelRoutingError
    from hermes_cli.runtime_provider import resolve_runtime_provider
    with pytest.raises(ModelRoutingError, match='cannot override'):
        resolve_runtime_provider(requested=ANTHROPIC[0], target_model=ANTHROPIC[1], explicit_api_key='sk-explicit',
                                 explicit_base_url=base_url)
    assert managed == []


def test_a_routing_refusal_on_a_local_chat_is_not_a_rejected_sign_in():
    from agent.error_surface import _REASON_TO_LAYER, LAYER_AUTH
    from agent.model_routing import ModelRoutingError
    from gateway.config import Platform
    from gateway.run_turn_runner import _resolution_failure_reply
    text, reason, retryable = _resolution_failure_reply(ModelRoutingError('no route'), Platform.LOCAL)
    assert 'no route' in text and retryable is False
    assert reason != 'auth_permanent' and _REASON_TO_LAYER.get(reason) not in (None, LAYER_AUTH)


def test_a_plain_credential_failure_on_a_local_chat_stays_a_sign_in_failure():
    from gateway.config import Platform
    from gateway.run_turn_runner import _resolution_failure_reply
    from hermes_cli.auth import AuthError
    assert _resolution_failure_reply(AuthError('no creds'), Platform.LOCAL)[1] == 'auth_permanent'
