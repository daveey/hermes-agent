"""[daveey] A kanban run on a class tier (``kanban.default_model: class:standard``) builds its agent.

The owner pins the dispatcher's tier as ``class:standard`` + provider ``auto`` (``owner_model_pin``).
The launch policy read that ``auto`` as the thin client's "no pick" and froze the profile's concrete
provider under the class, so every class-tier managed worker died at agent build with "Class
selectors require provider auto" (17 MacBook runs in 13 min after core a00432f5b3, 10-29).
"""
import pytest

PROFILE = {'model': {'default': 'claude-opus-5-5', 'provider': 'anthropic', 'base_url': 'https://api.anthropic.com',
                     'api_mode': 'anthropic_messages', 'context_length': 200000}}


@pytest.mark.parametrize('pick', [{'provider': 'auto'}, {'provider': 'Auto'}, {}], ids=['auto', 'Auto', 'no-pick'])
def test_a_class_selector_launch_runs_on_provider_auto(tmp_path, pick):
    from gateway.session_policy import build_policy
    policy = build_policy(dict(cwd=str(tmp_path), model='class:standard', **pick), PROFILE)
    model_cfg = policy.config()['model']
    assert (policy.model, policy.provider) == ('class:standard', 'auto')
    # The concrete route's pins belong to the provider the class replaced.
    assert not {'base_url', 'api_mode', 'context_length'} & set(model_cfg), model_cfg


def test_auto_on_a_concrete_model_still_means_the_profiles_route(tmp_path):
    """Upstream's thin-client sentinel is unchanged for concrete models."""
    from gateway.session_policy import build_policy
    policy = build_policy(dict(cwd=str(tmp_path), model='claude-opus-5-5', provider='auto'), PROFILE)
    assert (policy.provider, policy.base_url) == ('anthropic', 'https://api.anthropic.com')


def _kanban_policy(tmp_path, monkeypatch, **overrides):
    from contextlib import closing
    from types import SimpleNamespace
    from gateway.session_contract import Principal
    from gateway.session_kanban import build_kanban_policy
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda name: str(tmp_path))
    monkeypatch.setattr('hermes_cli.kanban_capabilities._profile_capabilities',
                        lambda name, *_required: {'terminal', 'file', 'git', 'host_repo', 'kanban'})
    kb.create_board('owned')
    with closing(connect(board='owned')) as conn:
        task_id = kb.create_task(conn, title='t', assignee='default', workspace_kind='dir',
                                 workspace_path=str(tmp_path), **overrides)
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, task_id)
    connection = SimpleNamespace(authority=SimpleNamespace(profile_id=str(tmp_path)), native_owner=True,
                                 actor=Principal('owner', str(tmp_path), frozenset({'session:create'}), 'transport'))
    policy, _secrets = build_kanban_policy(connection, dict(board='owned', task_id=task.id, run_id=task.current_run_id,
                                                            claim_lock=task.claim_lock), PROFILE)
    return policy


def test_a_kanban_class_tier_owner_policy_runs_on_provider_auto(tmp_path, monkeypatch):
    policy = _kanban_policy(tmp_path, monkeypatch, model_override='class:standard')
    assert (policy.model, policy.provider, policy.base_url) == ('class:standard', 'auto', None)


def test_the_managed_worker_leaves_a_class_route_to_the_agents_sticky_allocator(tmp_path, monkeypatch):
    """``worker_route`` must not pre-resolve a class (no session id there) nor hand the agent explicit
    credentials, which class routing refuses outside a managed policy: AIAgent's prepare_agent_route
    allocates it under the session id, as the CLI's ``-m class:<level> --provider auto`` does."""
    from gateway.session_worker_construct import worker_route
    policy = _kanban_policy(tmp_path, monkeypatch, model_override='class:standard')

    def resolver_called(*_a, **_k):
        raise AssertionError('a class selector was resolved outside the agent')
    monkeypatch.setattr('hermes_cli.runtime_provider.resolve_runtime_with_fallback', resolver_called)
    monkeypatch.setattr('hermes_cli.runtime_provider.resolve_runtime_provider', resolver_called)
    assert worker_route(policy, {'api_key': None}) == {'api_key': None, 'base_url': None, 'api_mode': None}


def test_a_launch_key_on_a_class_route_is_still_refused(tmp_path, monkeypatch):
    from agent.model_routing import ModelRoutingError
    from gateway.session_worker_construct import worker_route
    policy = _kanban_policy(tmp_path, monkeypatch, model_override='class:standard')
    with pytest.raises(ModelRoutingError, match='explicit credentials'):
        worker_route(policy, {'api_key': 'sk-launch'})
