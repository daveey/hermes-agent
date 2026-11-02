"""A committed switch intent replaces an allocation at most once, under the allocator transaction. [daveey]

Reachable overlap: a cold ``/review``/``/refine`` builds a same-session AIAgent while the durable queue
drains the session's next turn; the adapter's idle guard reserves nothing, so both builds can read the same
committed intent before either allocates. With read -> replace -> compare-delete split across two stores,
both builds replaced the allocation (two round-robin choices, possibly two different providers) and only one
consume won. The fence records which intent a ``(root, class)`` allocation already honoured inside the
allocator's ``BEGIN IMMEDIATE`` transaction, so a second build (or a crash retry before consume) is sticky.
"""
import sqlite3
import threading
from unittest.mock import patch

import pytest

import agent.model_routing_switch_intent as switch_intent
from agent.model_routing import ModelRoutingError, resolve_model_route
from agent.model_routing_state import RoutingState
from agent.model_routing_switch_intent import PREFIX, record_in_transaction
from hermes_state import SessionDB
from tests.agent import test_route_switch_intent as base

managed = base.managed
routing_home = base.routing_home
streaming_route = base.streaming_route
destinations = base.destinations
seams = base.seams
_build = base._build


def _choices(path):
    """Total round-robin choices the allocator has made (every replacement makes one)."""
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT COALESCE(SUM(position), 0) FROM counters").fetchone()[0]


def _overlap_after_pending(parties):
    """Hold every build after it has read the intent, before any allocates (the reachable window)."""
    barrier = threading.Barrier(parties, timeout=20)
    real = switch_intent.pending

    def pending(*args, **kwargs):
        raw = real(*args, **kwargs)
        barrier.wait()
        return raw
    return patch.object(switch_intent, 'pending', pending)


def _run_threads(target, names):
    results, errors = {}, []

    def run(name):
        try:
            results[name] = target(name)
        except BaseException as exc:  # surfaced below
            errors.append(exc)
    threads = [threading.Thread(target=run, args=(n,), name=n) for n in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors, errors
    return results


def test_composed_same_session_builds_honour_one_intent_once(destinations, tmp_path, seams):
    """Real AIAgent builds (cold review + drained turn) on one SessionDB intent replace once."""
    _build('anthropic-deep')
    routes = tmp_path / 'model-routing.sqlite3'
    before = _choices(routes)
    db = SessionDB(tmp_path / 'state-under-test.db')
    try:
        db._execute_write(lambda conn: record_in_transaction(conn, 'pin', 'z-anthropic-deep', 'anthropic', 2))
        with _overlap_after_pending(2):
            agents = _run_threads(lambda name: _build('z-anthropic-deep', session_db=db),
                                  ['cold_review', 'drain_turn'])
        assert _choices(routes) == before + 1  # one replacement allocation, not two
        assert agents['cold_review'].model_routing == agents['drain_turn'].model_routing
        recorded = RoutingState(routes).peek('pin', 'deep')
        assert (recorded['selected_model'], recorded['exact_pin'], recorded['reason']) == (
            'z-anthropic-deep', True, 'explicit-switch')
        assert db.get_meta(PREFIX + 'pin') is None
    finally:
        db.close()


def _two_provider_policy():
    route = {"billing": "subscription", "weight": 1, "eligible": True, "funding_verified": True,
             "capabilities": {"tools": True, "streaming": True, "context_tokens": 200000}}
    return {"model_routing": {"enabled": True, "policy_version": 1, "allow_metered": False, "routes": {
        "deep": [dict(route, provider=p, model=f"{p}-deep") for p in ("anthropic", "openai-codex")]}}}


class _Agent:
    pass


def _resolve_without_credentials(**kw):
    found = resolve_model_route(kw['target_model'], kw['requested'], config=_two_provider_policy(),
                                session_id=kw['routing_session_id'], state_path=kw['routing_config']['routes'],
                                replace=kw['routing_replace'])
    return {"model": found["model"], "provider": found["provider"], "api_key": "", "base_url": "",
            "api_mode": "", "credential_pool": None, "model_routing": found["metadata"]}


def _class_build(db, routes, session='s1'):
    """prepare_agent_route on the real intent store and allocator; credentials are out of scope here."""
    import agent.model_routing_agent as mra
    agent = _Agent()
    mra.prepare_agent_route(agent, 'class:deep', 'anthropic', session, None, None, None, None, None,
                            session_db=db)
    return agent


@pytest.fixture
def class_session(tmp_path):
    """Seams are patched once, on the test thread: patching inside overlapping threads leaks a stub."""
    routes = tmp_path / 'model-routing.sqlite3'
    resolve_model_route('class:deep', 'auto', config=_two_provider_policy(), session_id='s1', state_path=routes)
    db = SessionDB(tmp_path / 'state.db')
    db._execute_write(lambda conn: record_in_transaction(conn, 's1', 'class:deep', 'anthropic', 7))
    with patch('hermes_cli.runtime_provider.resolve_runtime_provider', _resolve_without_credentials), \
         patch('agent.model_routing_managed.managed_config', lambda *a, **k: None), \
         patch('hermes_cli.config_effective.load_user_config_effective', lambda: {'routes': routes}):
        yield db, routes
    db.close()


def test_concurrent_class_intent_cannot_split_providers(class_session):
    db, routes = class_session
    before = _choices(routes)
    with _overlap_after_pending(2):
        agents = _run_threads(lambda name: _class_build(db, routes), ['cold_review', 'drain_turn'])
    assert _choices(routes) == before + 1
    providers = {name: a.model_routing['selected_provider'] for name, a in agents.items()}
    durable = RoutingState(routes).peek('s1', 'deep')
    assert set(providers.values()) == {durable['selected_provider']}, (providers, durable)
    assert durable['reason'] == 'explicit-switch'
    assert db.route_switch_intent('s1') is None


def test_crash_between_allocation_and_consume_retries_without_a_second_replacement(class_session):
    db, routes = class_session

    def crash(*args, **kwargs):
        raise RuntimeError('process died before consume')
    with patch.object(switch_intent, 'consume', crash):
        with pytest.raises(RuntimeError, match='before consume'):
            _class_build(db, routes)
    committed = RoutingState(routes).peek('s1', 'deep')
    choices = _choices(routes)
    assert db.route_switch_intent('s1') is not None  # retry still owns the intent
    retried = _class_build(db, routes)
    assert retried.model_routing == committed
    assert _choices(routes) == choices
    assert db.route_switch_intent('s1') is None


def test_failover_after_honoured_intent_is_not_undone_by_a_lingering_intent(class_session):
    db, routes = class_session
    with patch.object(switch_intent, 'consume', lambda *a, **k: None):  # consume lost
        first = _class_build(db, routes).model_routing
    failed = (first['selected_provider'], first['selected_model'])
    over = resolve_model_route('class:deep', 'auto', config=_two_provider_policy(), session_id='s1',
                               state_path=routes, excluded=[failed])
    assert over['provider'] != first['selected_provider']
    assert _class_build(db, routes).model_routing == over['metadata']


def test_a_newer_committed_intent_replaces_again(class_session):
    db, routes = class_session
    _class_build(db, routes)
    choices = _choices(routes)
    db._execute_write(lambda conn: record_in_transaction(conn, 's1', 'class:deep', 'anthropic', 8))
    _class_build(db, routes)
    assert _choices(routes) == choices + 1
    assert db.route_switch_intent('s1') is None


def test_unfenced_replace_stays_unconditional(tmp_path):
    """In-session ``/model`` switches pass ``replace=True``; they are not keyed to an intent."""
    path = tmp_path / 'routes.db'
    args = dict(config=_two_provider_policy(), session_id='s', state_path=path)
    resolve_model_route('class:deep', 'auto', **args)
    for expected in (2, 3):
        resolve_model_route('class:deep', 'auto', replace=True, **args)
        assert _choices(path) == expected


def test_ordinary_pin_without_intent_still_refuses(destinations, tmp_path, seams):
    _build('anthropic-deep')
    routes = tmp_path / 'model-routing.sqlite3'
    before = RoutingState(routes).peek('pin', 'deep')
    db = SessionDB(tmp_path / 'state-under-test.db')
    try:
        with pytest.raises(ModelRoutingError, match='cannot fail over'):
            _build('z-anthropic-deep', session_db=db)
        assert RoutingState(routes).peek('pin', 'deep') == before
    finally:
        db.close()


def test_auth_failure_rolls_back_the_fence_and_a_retry_replaces(destinations, tmp_path, seams):
    _build('anthropic-deep')
    routes = tmp_path / 'model-routing.sqlite3'
    before, choices = RoutingState(routes).peek('pin', 'deep'), _choices(routes)
    db = SessionDB(tmp_path / 'state-under-test.db')
    try:
        db._execute_write(lambda conn: record_in_transaction(conn, 'pin', 'z-anthropic-deep', 'anthropic', 2))
        with patch('agent.model_routing_runtime.verify_class_runtime',
                   side_effect=ModelRoutingError('credential refused')):
            with pytest.raises(ModelRoutingError, match='credential refused'):
                _build('z-anthropic-deep', session_db=db)
        assert RoutingState(routes).peek('pin', 'deep') == before
        assert _choices(routes) == choices
        assert db.get_meta(PREFIX + 'pin') is not None
        assert _build('z-anthropic-deep', session_db=db).model == 'z-anthropic-deep'
        assert _choices(routes) == choices + 1
        assert db.get_meta(PREFIX + 'pin') is None
    finally:
        db.close()


def test_concurrent_auth_failure_leaves_the_other_build_free_to_replace(destinations, tmp_path, seams):
    """A refused credential in one overlapping build must not fence the intent for the other."""
    _build('anthropic-deep')
    routes = tmp_path / 'model-routing.sqlite3'
    db = SessionDB(tmp_path / 'state-under-test.db')
    try:
        db._execute_write(lambda conn: record_in_transaction(conn, 'pin', 'z-anthropic-deep', 'anthropic', 2))
        import agent.model_routing_runtime as runtime
        real = runtime.verify_class_runtime

        def verify(rt, metadata):
            if threading.current_thread().name == 'refused':
                raise ModelRoutingError('credential refused')
            return real(rt, metadata)

        def build(name):
            try:
                return _build('z-anthropic-deep', session_db=db)
            except ModelRoutingError as exc:
                return exc
        with patch.object(runtime, 'verify_class_runtime', verify), _overlap_after_pending(2):
            results = _run_threads(build, ['refused', 'accepted'])
        assert isinstance(results['refused'], ModelRoutingError)
        assert results['accepted'].model == 'z-anthropic-deep'
        assert RoutingState(routes).peek('pin', 'deep')['selected_model'] == 'z-anthropic-deep'
        assert db.get_meta(PREFIX + 'pin') is None
    finally:
        db.close()
