"""Admission → pace → real owner spawn → worker allocation, fake provider adapters only."""
import json
from types import SimpleNamespace

import pytest

from agent import managed_worker
from agent.model_routing import ModelRoutingError
from gateway import session_kanban_scope as scope
from gateway import session_worker_construct as construct
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_pace_reroute as pace
from hermes_cli import kanban_refusal_fallback as refusal
from tests.hermes_cli import test_kanban_refusal_fallback as fixtures

board = fixtures.board
pools = fixtures.pools


@pytest.fixture
def claimed(board, pools, monkeypatch):
    conn, context = board
    # Frozen historical receipt, not a provider call or a replay of refused work.
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_runs(task_id,status,started_at,ended_at,outcome,metadata) "
                     "VALUES (?, 'safety_refused', 1, 2, 'safety_refused', ?)",
                     (context['task_id'], json.dumps({'refused_provider': 'anthropic'})))
    task = kb.get_task(conn, context['task_id'])
    pace.clear()
    yield conn, task, context
    pace.clear()


def policy_for(context, provider='openai-codex', model='gpt-test'):
    return SimpleNamespace(kanban_json=json.dumps(context), model=model, provider=provider)


def test_pace_offer_cannot_overwrite_refusal_filter(claimed):
    conn, task, _ = claimed
    route = tier.resolve_dispatch_tier(conn, task, lane='ready')
    assert route[1] == 'openai-codex'
    pace.offer(task.id, route, ('sonnet-test', 'anthropic', 'openai-codex'))
    with pytest.raises(ModelRoutingError, match='Safety exclusion'):
        tier.apply_model_tier(conn, task, lane='ready')
    assert not fixtures.events(conn, task.id, 'model_tier')
    assert refusal.refused_providers(conn, task.id) == ['anthropic']


def test_peer_lost_during_pace_consumption_fails_closed(claimed, monkeypatch):
    conn, task, _ = claimed
    take = pace.take

    def disappear(*args):
        fixtures.UNSERVED.add('openai-codex')
        return take(*args)

    monkeypatch.setattr(pace, 'take', disappear)
    with pytest.raises(ModelRoutingError, match='no longer eligible'):
        tier.apply_model_tier(conn, task, lane='ready')
    assert not fixtures.events(conn, task.id, 'model_tier')


@pytest.mark.parametrize('provider,model', [('anthropic', 'sonnet-test'), ('auto', 'class:standard')])
def test_real_owner_spawn_rejects_excluded_or_deferred_route(claimed, monkeypatch, provider, model):
    _, _, context = claimed
    called = []
    monkeypatch.setattr(scope, 'resolve_run_scope', lambda _: None)
    launch = scope.placing_spawn(lambda *a, **kw: called.append((a, kw)), policy_for(context, provider, model))
    with pytest.raises(ModelRoutingError, match='Safety exclusion'):
        launch(['fake-provider-worker'])
    assert called == []


def test_owner_spawn_rechecks_peer_after_admission(claimed, monkeypatch):
    conn, task, context = claimed
    tier.apply_model_tier(conn, task, lane='ready')
    fixtures.UNSERVED.add('openai-codex')
    called = []
    monkeypatch.setattr(scope, 'resolve_run_scope', lambda _: None)
    with pytest.raises(ModelRoutingError, match='no longer eligible'):
        scope.placing_spawn(lambda *a, **kw: called.append(True), policy_for(context))(['fake'])
    assert called == []


def test_owner_spawn_accepts_eligible_unrefused_route(claimed, monkeypatch):
    _, _, context = claimed
    sentinel = object()
    monkeypatch.setattr(scope, 'resolve_run_scope', lambda _: None)
    assert scope.placing_spawn(lambda *a, **kw: sentinel, policy_for(context))(['fake']) is sentinel


@pytest.mark.parametrize('actual', ['anthropic', None, 'openai-codex'])
def test_worker_allocation_is_checked_before_execution(claimed, monkeypatch, actual):
    _, _, context = claimed
    policy = policy_for(context)
    frame = {'policy': {'kanban_json': policy.kanban_json}}
    calls = []
    monkeypatch.setattr(construct, 'worker_route', lambda *a: {})
    monkeypatch.setattr('gateway.session_kanban.record_start_failure', lambda *a: None)

    def factory(**kwargs):
        from agent.model_routing_exclusions import provider_allowed
        assert not provider_allowed('anthropic') and provider_allowed('openai-codex')
        calls.append(kwargs)
        return SimpleNamespace(provider=actual, model='gpt-test',
                               shutdown_memory_provider=lambda _: None, release_clients=lambda: None)

    if actual != 'openai-codex':
        with pytest.raises(ModelRoutingError, match='Safety exclusion'):
            managed_worker._construct_agent(frame, factory, policy, model='gpt-test', provider='openai-codex')
    else:
        agent = managed_worker._construct_agent(frame, factory, policy, model='gpt-test', provider='openai-codex')
        assert agent.provider == actual
    assert len(calls) == 1


def test_worker_refuses_new_exclusion_before_adapter_called(claimed, monkeypatch):
    _, _, context = claimed
    policy = policy_for(context, 'anthropic', 'sonnet-test')
    calls = []
    monkeypatch.setattr(construct, 'worker_route', lambda *a: {})
    monkeypatch.setattr('gateway.session_kanban.record_start_failure', lambda *a: None)
    with pytest.raises(ModelRoutingError, match='Safety exclusion'):
        managed_worker._construct_agent({'policy': {'kanban_json': policy.kanban_json}},
                                       lambda **kw: calls.append(kw), policy)
    assert calls == []


def test_request_boundary_rejects_late_provider_change(claimed, monkeypatch):
    from agent import model_routing_managed as managed
    _, _, context = claimed
    agent = SimpleNamespace(model='gpt-test', provider='anthropic', _kanban_refusal_policy=policy_for(context))
    monkeypatch.setattr(managed, 'managed_config', lambda: None)
    with pytest.raises(ModelRoutingError, match='Safety exclusion'):
        managed.verify_managed_agent(agent, {'model': 'gpt-test'})


def test_resolution_error_does_not_fall_back_to_profile(claimed, monkeypatch):
    conn, task, _ = claimed
    def unavailable(*a, **kw):
        raise RuntimeError('fake policy reader unavailable')
    monkeypatch.setattr(tier, 'resolve_dispatch_tier', unavailable)
    with pytest.raises(ModelRoutingError, match='concrete execution route'):
        tier.apply_model_tier(conn, task, lane='ready')


def test_unknown_eligibility_refuses_instead_of_assuming_available(claimed, monkeypatch):
    from hermes_cli.kanban_refusal_boundary import require_policy
    _, _, context = claimed
    def unavailable(*a, **kw):
        raise OSError('fake eligibility unavailable')
    monkeypatch.setattr(refusal, 'eligible_routes', unavailable)
    with pytest.raises(ModelRoutingError, match='eligibility unavailable'):
        require_policy(policy_for(context))


@pytest.mark.parametrize('field,value', [('status', 'done'), ('claim_lock', 'foreign'), ('ended_at', 10)])
def test_refused_history_requires_live_exact_run(claimed, field, value):
    from hermes_cli.kanban_refusal_boundary import require_policy
    conn, _, context = claimed
    with kb.write_txn(conn):
        conn.execute(f'UPDATE task_runs SET {field}=? WHERE id=?', (value, context['run_id']))
    with pytest.raises(ModelRoutingError, match='claim is stale'):
        require_policy(policy_for(context))


def test_missing_actual_model_never_uses_policy_default(claimed):
    from hermes_cli.kanban_refusal_boundary import require_policy
    _, _, context = claimed
    with pytest.raises(ModelRoutingError, match='concrete execution route'):
        require_policy(policy_for(context), model=None)


def test_worker_route_fallback_refused_before_factory(claimed, monkeypatch):
    _, _, context = claimed
    policy = policy_for(context)
    monkeypatch.setattr(construct, 'worker_route', lambda *a: {'provider': 'anthropic'})
    monkeypatch.setattr('gateway.session_kanban.record_start_failure', lambda *a: None)
    called = []
    with pytest.raises(ModelRoutingError, match='Safety exclusion'):
        managed_worker._construct_agent({'policy': {'kanban_json': policy.kanban_json}},
                                       lambda **kw: called.append(kw), policy, model='gpt-test')
    assert called == []


def test_unreadable_history_never_reaches_factory(claimed, monkeypatch, tmp_path):
    _, _, context = claimed
    policy = policy_for({**context, 'db': str(tmp_path / 'missing.db')})
    monkeypatch.setattr('gateway.session_kanban.record_start_failure', lambda *a: None)
    called = []
    with pytest.raises(ModelRoutingError, match='history unavailable'):
        managed_worker._construct_agent({'policy': {'kanban_json': policy.kanban_json}},
                                       lambda **kw: called.append(kw), policy)
    assert called == []
