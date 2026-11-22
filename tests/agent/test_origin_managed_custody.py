"""Managed interactive attribution uses adopted custody, not process environment."""
from pathlib import Path

import pytest

from tests.gateway import test_worker_turn_lease_query as lease_tests
from tests.gateway.test_worker_native_origin_query import _compress

worker = lease_tests.worker
OWNED = {'origin': {'gateway': 'here', 'profile': 'default', 'session': 'owned'}}


@pytest.fixture
def managed(worker, monkeypatch):
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, 'gateway_names', lambda: ('here', []))
    monkeypatch.setattr('agent.turn_liveness.resolve_turn_liveness_settings', lambda cfg: (None, 1.0))
    monkeypatch.setattr('agent.relay_cwd.resolve_relay_scope_cwds', lambda *a: ('', ''))
    return worker


def _agent(store, *, bind=True):
    from agent.origin_managed_custody import bind_adopted_agent
    from hermes_constants import get_hermes_home
    from tests.agent.test_cross_process_turn_lease import _agent_with_db
    agent = _agent_with_db(store, session_id=store.scope['session_id'], platform='tui')
    if bind:
        bind_adopted_agent(agent, store, home=get_hermes_home(), scope=dict(store.scope))
    return agent


def _turn(agent, monkeypatch, inside):
    """Run the actual facade; ``inside`` executes during the live leased turn."""
    seen = []

    def loop(*args, **kwargs):
        seen.append(inside())
        return dict(final_response='ok', messages=[], failed=False)

    monkeypatch.setattr('agent.conversation_loop.run_conversation', loop)
    agent.run_conversation('capture')
    assert len(seen) == 1
    return seen[0]


def _capture():
    from hermes_cli import kanban_origin_native as native
    value = native.capture(None)
    return value, (value.validated() if value is not None else None)


def test_managed_interactive_facade_capture(managed, monkeypatch):
    from agent import origin_turn_custody
    store = managed.store
    agent = _agent(store)

    def inside():
        assert origin_turn_custody._current.get()[0].db is store
        return _capture()

    value, validated = _turn(agent, monkeypatch, inside)
    assert validated == OWNED
    assert value.validated() is None        # revoked with the lease after the turn


def test_unbound_managed_agent_has_no_authority(managed, monkeypatch):
    """Without the private post-adoption capability the RPC store grants nothing."""
    assert _turn(_agent(managed.store, bind=False), monkeypatch, _capture) == (None, None)


@pytest.mark.parametrize(('source', 'created'), [
    ('cron', None), ('local', 'kanban'), ('local', None), ('cli', 'subagent'), ('telegram', None),
])
def test_noninteractive_creation_source_refused(managed, monkeypatch, source, created):
    managed.db._write_sql('UPDATE sessions SET source=?,created_source=? WHERE id=?', (source, created, 'owned'))
    assert _turn(_agent(managed.store), monkeypatch, _capture) == (None, None)


def test_normalized_local_desktop_session_is_interactive(managed, monkeypatch):
    managed.db._write_sql("UPDATE sessions SET source='local',created_source='desktop' WHERE id='owned'")
    assert _turn(_agent(managed.store), monkeypatch, _capture)[1] == OWNED


def test_owner_published_compression_keeps_current_session(managed, monkeypatch):
    store = managed.store
    agent = _agent(store)

    def inside():
        _compress(store, 'owned', 'child')
        assert store.scope['session_id'] == 'child'
        agent.session_id = 'child'
        return _capture()

    assert _turn(agent, monkeypatch, inside)[1] == {'origin': dict(OWNED['origin'], session='child')}


def test_agent_naming_retired_compression_target_refused(managed, monkeypatch):
    store = managed.store

    def inside():
        _compress(store, 'owned', 'child')
        return _capture()        # agent still names the retired target

    assert _turn(_agent(store), monkeypatch, inside) == (None, None)


def _bump_pid(managed, agent, store):
    cap = agent._native_origin_managed_custody
    object.__setattr__(cap, 'pid', cap.pid + 1)


def _replace_epoch(managed, agent, store):
    from hermes_state_runtime import begin_runtime_epoch
    begin_runtime_epoch(managed.db, instance_id='replacement')


_MUTATIONS = {
    'generation': lambda managed, agent, store: store.scope.update(generation=store.scope['generation'] + 1),
    'execution': lambda managed, agent, store: store.scope.update(execution_id='other'),
    'foreign_store': lambda managed, agent, store: setattr(agent, '_session_db', object()),
    'pid': _bump_pid,
    'persist_disabled': lambda managed, agent, store: setattr(agent, '_persist_disabled', True),
    'expired': lambda managed, agent, store: managed.db._write_sql('UPDATE session_turn_leases SET expires_at=1'),
    'stale_epoch': _replace_epoch,
    'holder': lambda managed, agent, store: managed.db._write_sql(
        "UPDATE session_turn_leases SET holder='intruder'"),
}


@pytest.mark.parametrize('mutate', sorted(_MUTATIONS))
def test_custody_predicates_fail_closed(managed, monkeypatch, mutate):
    store = managed.store
    agent = _agent(store)
    saved = dict(store.scope)

    def inside():
        _MUTATIONS[mutate](managed, agent, store)
        result = _capture()
        store.scope.clear()
        store.scope.update(saved)
        agent._session_db = store
        return result

    assert _turn(agent, monkeypatch, inside) == (None, None)


def test_bind_refuses_mismatch_and_double_binding(managed, tmp_path):
    from agent.origin_managed_custody import bind_adopted_agent
    from hermes_constants import get_hermes_home
    store = managed.store
    agent = _agent(store, bind=False)
    with pytest.raises(ValueError, match='invalid_managed_origin_agent'):
        bind_adopted_agent(agent, store, home=tmp_path / 'elsewhere', scope=dict(store.scope))
    with pytest.raises(ValueError, match='invalid_managed_origin_agent'):
        bind_adopted_agent(agent, store, home=get_hermes_home(), scope=dict(store.scope, generation=9))
    bind_adopted_agent(agent, store, home=get_hermes_home(), scope=dict(store.scope))
    with pytest.raises(ValueError, match='invalid_managed_origin_agent'):
        bind_adopted_agent(agent, store, home=get_hermes_home(), scope=dict(store.scope))
    # A non-RPC store (local SessionDB or a shim) never receives managed custody.
    other = _agent(store, bind=False)
    other._session_db = managed.db
    bind_adopted_agent(other, managed.db, home=Path(get_hermes_home()), scope=dict(store.scope))
    assert getattr(other, '_native_origin_managed_custody', None) is None


@pytest.mark.parametrize('kanban', [False, True])
def test_adopted_execute_binds_managed_custody_only_without_task_custody(tmp_path, monkeypatch, kanban):
    """Wiring proof for the private execute seam: adoption first, task custody wins."""
    from types import SimpleNamespace
    from agent import origin_managed_custody
    from agent.runtime_session_store import RuntimeSessionStore
    from gateway import session_kanban
    from tests.gateway.test_managed_worker_route_key import _execute
    from tests.gateway.test_managed_turn_contract import assignment
    frame, _ = assignment(tmp_path, monkeypatch)
    calls, task_bound = [], []
    capability = SimpleNamespace(bind_agent=task_bound.append) if kanban else None
    monkeypatch.setattr(session_kanban, 'bind_worker_context', lambda frame: capability)
    # This is a wiring-only mock, not a counterfeit native custody capability. Keep the restriction
    # registration seam observable without relaxing its production exact-WorkerCustody validation.
    registered = []
    monkeypatch.setattr('agent.kanban_delegation_policy.register_root',
                        lambda agent, original: registered.append((agent, original)))
    monkeypatch.setattr(origin_managed_custody, 'bind_adopted_agent',
                        lambda agent, store, **kw: calls.append((agent, store, kw)))

    def turns(agent, frame, history):
        if kanban:
            assert task_bound == [agent] and calls == []
            assert registered == [(agent, capability)]
        else:
            assert registered == []
            [(bound, store, kw)] = calls
            # The harness substitutes its store class; the seam must pass the one the agent was built on.
            assert bound is agent and store is built['session_db']
            assert type(store).__name__ == 'Store' or isinstance(store, RuntimeSessionStore)
            assert kw == {'home': frame['home'], 'scope': dict(frame['scope'])}
        return dict(final_response='ok', completed=True)

    monkeypatch.setattr(session_kanban, 'run_worker_turns', turns)
    built = {}
    _execute(frame, monkeypatch, built=built)
    assert len(calls) == (0 if kanban else 1)
