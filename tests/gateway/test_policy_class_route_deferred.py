"""[daveey] A launch-policy session on a ``class:`` model defers class resolution to the agent boundary.

``hermes chat -Q --oneshot --source tool`` on a profile whose default is ``class:deep`` failed every turn with
"Class routing requires a durable session/job identity": ``route_runtime`` resolved the selector with no session
id. Class allocation is sticky per session, so it belongs to ``AIAgent`` (``prepare_agent_route``), which owns the
session id, exactly as the messaging path defers it (``gateway.run_class_routing.class_session_runtime``).
"""
import pytest

CLASS_CONFIG = {'model': {'default': 'class:deep', 'provider': 'auto'},
                'model_routing': {'enabled': True, 'policy_version': 1, 'routes': {}}}


def _policy(tmp_path, config, **params):
    from gateway.session_policy import build_policy
    return build_policy({'source': 'tool', 'cwd': str(tmp_path), 'toolsets': [], **params}, config,
                        profile_terminal=False)


# The CLI sends its resolved model (``model='class:deep'``); another client may leave the profile default.
PICKS = [{'model': 'class:deep'}, {}]


@pytest.mark.parametrize('pick', PICKS, ids=['picked', 'profile-default'])
@pytest.mark.parametrize('fallback', [True, False])
def test_a_class_route_is_deferred_not_resolved_without_a_session(tmp_path, fallback, pick):
    from gateway.session_worker_construct import route_runtime
    policy = _policy(tmp_path, CLASS_CONFIG, **pick)
    frozen, model = policy.effective()
    runtime, fallback_entry = route_runtime(policy, frozen, model, None, fallback=fallback)
    assert fallback_entry is None
    assert (runtime['provider'], runtime['requested_provider']) == ('auto', 'auto')
    assert not runtime.get('api_key') and not runtime.get('base_url')


@pytest.mark.parametrize('pick', PICKS, ids=['picked', 'profile-default'])
def test_the_in_process_turn_hands_the_class_selector_to_the_agent(tmp_path, pick):
    from types import SimpleNamespace
    from gateway.run_turn_prepare import _resolve_policy_agent_runtime
    policy = _policy(tmp_path, CLASS_CONFIG, **pick)
    runner = SimpleNamespace(session_authority=None)
    model, kwargs = _resolve_policy_agent_runtime(runner, policy)
    assert model == 'class:deep'
    assert kwargs['provider'] == 'auto' and kwargs['api_key'] is None


def test_a_class_route_with_a_launch_key_still_refuses(tmp_path):
    from agent.model_routing import ModelRoutingError
    from gateway.session_worker_construct import route_runtime
    policy = _policy(tmp_path, CLASS_CONFIG, model='class:deep')
    frozen, model = policy.effective()
    with pytest.raises(ModelRoutingError):
        route_runtime(policy, frozen, model, 'sk-launch', fallback=False)
