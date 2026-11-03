"""A committed explicit model switch replaces the recorded allocation at the next build, once. [daveey]

Incident: the canonical ``model`` mutation moved ``sessions.model`` to claude-opus while the allocation
store (and ``model_config.model_routing``) still held an exact pin on gpt-astra, so the next build either
refused ("Exact pinned route cannot fail over") or a reader restored the OLD pin.
"""
from unittest.mock import MagicMock, patch

import pytest

from agent.model_routing import ModelRoutingError
from agent.model_routing_state import RoutingState
from agent.model_routing_switch_intent import PREFIX, record_in_transaction
from hermes_state import SessionDB
from run_agent import AIAgent
from tests.agent import test_managed_switch as switch_fixtures

managed = switch_fixtures.managed
routing_home = switch_fixtures.routing_home
streaming_route = switch_fixtures.streaming_route
destinations = switch_fixtures.destinations


def _build(model, session='pin', session_db=None):
    return AIAgent(model=model, provider='auto', session_id=session, session_db=session_db,
                   quiet_mode=True, skip_memory=True, skip_context_files=True)


@pytest.fixture
def seams():
    with patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('agent.anthropic_adapter.build_anthropic_client', return_value=MagicMock()):
        yield


def _commit_intent(path, session, model, provider='anthropic'):
    db = SessionDB(path)
    db._execute_write(lambda conn: record_in_transaction(conn, session, model, provider, 2))
    db.close()


def test_committed_switch_survives_restart_and_replaces_the_exact_pin(destinations, tmp_path, seams):
    _build('anthropic-deep')
    state = RoutingState(tmp_path / 'model-routing.sqlite3')
    assert state.peek('pin', 'deep')['selected_model'] == 'anthropic-deep'
    _commit_intent(tmp_path / 'state-under-test.db', 'pin', 'z-anthropic-deep')
    restarted = SessionDB(tmp_path / 'state-under-test.db')  # a fresh owner process reads the receipt
    try:
        agent = _build('z-anthropic-deep', session_db=restarted)
        assert agent.model == 'z-anthropic-deep'
        recorded = state.peek('pin', 'deep')
        assert (recorded['selected_model'], recorded['exact_pin'], recorded['reason']) == (
            'z-anthropic-deep', True, 'explicit-switch')
        assert restarted.get_meta(PREFIX + 'pin') is None  # one-shot
        # The new pin is now itself exact: the old model cannot come back without its own switch.
        with pytest.raises(ModelRoutingError, match='cannot fail over'):
            _build('anthropic-deep', session_db=restarted)
    finally:
        restarted.close()


def test_managed_worker_store_honors_committed_switch(destinations, tmp_path, seams):
    """The production child has an RPC facade, not the owner's SessionDB."""
    from tests.agent.test_runtime_session_store_surface_parity import _worker_store

    _build('anthropic-deep')
    db = SessionDB(tmp_path / 'state-under-test.db')
    try:
        db.create_session('pin', 'cli', model='z-anthropic-deep')
        db._execute_write(lambda conn: record_in_transaction(
            conn, 'pin', 'z-anthropic-deep', 'anthropic', 2))
        store = _worker_store(db, tmp_path, session_id='pin')
        try:
            agent = _build('z-anthropic-deep', session_db=store)
            assert agent.model == 'z-anthropic-deep'
            assert db.get_meta(PREFIX + 'pin') is None
        finally:
            store.close()
    finally:
        db.close()


def _worker_db(tmp_path):
    db = SessionDB(tmp_path / 'state-under-test.db')
    db.create_session('pin', 'cli', model='anthropic-deep')
    db.create_session('other', 'cli', model='anthropic-deep')
    return db


def test_managed_worker_store_refuses_another_sessions_intent(tmp_path):
    """Owner-only, assignment-bound: a worker for ``other`` can neither read nor consume ``pin``'s intent."""
    from agent.runtime_session_store import WorkerPersistenceError
    from tests.agent.test_runtime_session_store_surface_parity import _worker_store
    db = _worker_db(tmp_path)
    try:
        db._execute_write(lambda conn: record_in_transaction(conn, 'pin', 'z-anthropic-deep', 'anthropic', 2))
        raw = db.route_switch_intent('pin')
        store = _worker_store(db, tmp_path, session_id='other')
        try:
            with pytest.raises(WorkerPersistenceError, match='permission_denied'):
                store.route_switch_intent('pin')
            with pytest.raises(WorkerPersistenceError, match='permission_denied'):
                store.consume_route_switch_intent('pin', raw)
            assert store.route_switch_intent('other') is None
            assert store.consume_route_switch_intent('other', raw) is False  # own key only
        finally:
            store.close()
        assert db.route_switch_intent('pin') == raw
    finally:
        db.close()


def test_managed_worker_consume_is_compare_and_delete(tmp_path):
    """A newer switch committed between the worker's read and its consume survives."""
    from tests.agent.test_runtime_session_store_surface_parity import _worker_store
    db = _worker_db(tmp_path)
    try:
        db._execute_write(lambda conn: record_in_transaction(conn, 'pin', 'z-anthropic-deep', 'anthropic', 2))
        store = _worker_store(db, tmp_path, session_id='pin')
        try:
            old = store.route_switch_intent('pin')
            db._execute_write(lambda conn: record_in_transaction(conn, 'pin', 'anthropic-standard', 'anthropic', 3))
            assert store.consume_route_switch_intent('pin', old) is False
            newer = db.route_switch_intent('pin')
            assert newer != old and 'anthropic-standard' in newer
            assert store.consume_route_switch_intent('pin', newer) is True
            assert db.route_switch_intent('pin') is None
        finally:
            store.close()
    finally:
        db.close()


@pytest.mark.parametrize('model,provider', [('z-anthropic-deep', 'openai-codex'), ('anthropic-standard', 'anthropic')],
                         ids=['wrong-provider', 'class-change'])
def test_managed_worker_build_with_a_non_matching_intent_keeps_the_pin(destinations, tmp_path, seams, model, provider):
    from tests.agent.test_runtime_session_store_surface_parity import _worker_store
    _build('anthropic-deep')
    state = RoutingState(tmp_path / 'model-routing.sqlite3')
    before = state.peek('pin', 'deep')
    db = _worker_db(tmp_path)
    try:
        db._execute_write(lambda conn: record_in_transaction(conn, 'pin', model, provider, 2))
        store = _worker_store(db, tmp_path, session_id='pin')
        try:
            # wrong-provider: the switch's provider cannot serve the model (refused); class-change: no match.
            with pytest.raises(ModelRoutingError):
                _build('z-anthropic-deep', session_db=store)
        finally:
            store.close()
        assert state.peek('pin', 'deep') == before
        assert db.route_switch_intent('pin') is not None
    finally:
        db.close()


def test_managed_worker_failed_allocation_keeps_the_intent(destinations, tmp_path, seams):
    from tests.agent.test_runtime_session_store_surface_parity import _worker_store
    _build('anthropic-deep')
    state = RoutingState(tmp_path / 'model-routing.sqlite3')
    before = state.peek('pin', 'deep')
    db = _worker_db(tmp_path)
    try:
        db._execute_write(lambda conn: record_in_transaction(conn, 'pin', 'z-anthropic-deep', 'anthropic', 2))
        store = _worker_store(db, tmp_path, session_id='pin')
        try:
            with patch('agent.model_routing_runtime.verify_class_runtime',
                       side_effect=ModelRoutingError('credential refused')):
                with pytest.raises(ModelRoutingError, match='credential refused'):
                    _build('z-anthropic-deep', session_db=store)
            assert state.peek('pin', 'deep') == before
            assert db.route_switch_intent('pin') is not None
            assert _build('z-anthropic-deep', session_db=store).model == 'z-anthropic-deep'
            assert db.route_switch_intent('pin') is None
        finally:
            store.close()
    finally:
        db.close()


def test_model_switch_row_writer_moves_the_whole_route_and_billing(tmp_path, monkeypatch):
    """The receipt row writer replaces stale endpoint/api_mode/gateway_runtime/billing with the picked tuple."""
    import json
    import hermes_state_mutation_prepared as prepared_mod
    db = SessionDB(tmp_path / 'state-under-test.db')
    try:
        db.create_session('pin', 'cli', model='gpt-old', model_config={
            'provider': 'openai-codex', 'base_url': 'https://chatgpt.com/backend-api/codex',
            'api_mode': 'codex_responses', 'keep': 1, 'model_routing': {'exact_pin': True},
            'gateway_runtime': {'provider': 'openai-codex', 'base_url': 'https://chatgpt.com/backend-api/codex',
                                'api_mode': 'codex_responses'}})
        db._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET billing_provider='openai-codex', billing_base_url='https://chatgpt.com/backend-api/codex',"
            " billing_mode='codex_responses' WHERE id='pin'"))
        monkeypatch.setattr(prepared_mod, '_commit_policy', lambda db, conn, sid, prepared: ({'target': sid}, 2))
        prepared = {'policy': {'model': 'claude-opus-5-5', 'config_json': json.dumps({'model': {
            'provider': 'anthropic', 'base_url': 'https://api.anthropic.com', 'default': 'claude-opus-5-5'}})}}
        db._execute_write(lambda conn: prepared_mod.model_in_transaction(db, conn, 'pin', {'model': 'claude-opus-5-5'},
                                                                          prepared))
        row = db.get_session('pin')
        config = json.loads(row['model_config'])
        route = {'provider': 'anthropic', 'base_url': 'https://api.anthropic.com'}
        assert row['model'] == 'claude-opus-5-5'
        # Same shape SessionDB.update_session_model writes (top-level None deletes; gateway_runtime keeps None).
        assert config == {'keep': 1, 'model': 'claude-opus-5-5', **route,
                          'gateway_runtime': {**route, 'api_mode': None}}
        assert (row['billing_provider'], row['billing_base_url']) == ('anthropic', 'https://api.anthropic.com')
        assert 'claude-opus-5-5' in db.route_switch_intent('pin')
    finally:
        db.close()


def test_route_runtime_prebuild_lookup_records_no_allocation(destinations, tmp_path, monkeypatch):
    """Gateway and worker pre-resolution (route_runtime) must not mint standing allocations."""
    import sqlite3
    from gateway import session_worker_construct as construct
    from hermes_cli.runtime_provider import resolve_runtime_provider

    def lookup(policy, frozen, model, key, *, fallback=True):
        return resolve_runtime_provider(requested='anthropic', target_model='anthropic-deep'), None
    monkeypatch.setattr(construct, '_route_runtime', lookup)
    for fallback in (True, False):  # in-process turn and worker_route both come through here
        runtime, _ = construct.route_runtime(None, {}, 'anthropic-deep', None, fallback=fallback)
        assert runtime['model'] == 'anthropic-deep'
    path = tmp_path / 'model-routing.sqlite3'
    if path.exists():
        with sqlite3.connect(path) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert 'routes' not in tables or conn.execute('SELECT COUNT(*) FROM routes').fetchone()[0] == 0
    # Control: the same lookup outside route_runtime does allocate, so the check above can fail.
    lookup(None, {}, 'anthropic-deep', None)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM routes').fetchone()[0] == 1


def test_no_intent_cannot_override_an_exact_pin(destinations, tmp_path, seams):
    _build('anthropic-deep')
    state = RoutingState(tmp_path / 'model-routing.sqlite3')
    before = state.peek('pin', 'deep')
    db = SessionDB(tmp_path / 'state-under-test.db')
    try:
        with pytest.raises(ModelRoutingError, match='cannot fail over'):
            _build('z-anthropic-deep', session_db=db)
        assert state.peek('pin', 'deep') == before
    finally:
        db.close()


@pytest.mark.parametrize('session,model', [('other', 'z-anthropic-deep'), ('pin', 'anthropic-standard')])
def test_an_intent_for_another_session_or_tuple_does_not_move_the_pin(destinations, tmp_path, seams, session, model):
    _build('anthropic-deep')
    state = RoutingState(tmp_path / 'model-routing.sqlite3')
    before = state.peek('pin', 'deep')
    _commit_intent(tmp_path / 'state-under-test.db', session, model)
    db = SessionDB(tmp_path / 'state-under-test.db')
    try:
        with pytest.raises(ModelRoutingError, match='cannot fail over'):
            _build('z-anthropic-deep', session_db=db)
        assert state.peek('pin', 'deep') == before
        assert db.get_meta(PREFIX + session) is not None  # never consumed by a non-matching build
    finally:
        db.close()


def test_failed_allocation_rolls_back_and_keeps_the_intent(destinations, tmp_path, seams):
    _build('anthropic-deep')
    state = RoutingState(tmp_path / 'model-routing.sqlite3')
    before = state.peek('pin', 'deep')
    _commit_intent(tmp_path / 'state-under-test.db', 'pin', 'z-anthropic-deep')
    db = SessionDB(tmp_path / 'state-under-test.db')
    try:
        with patch('agent.model_routing_runtime.verify_class_runtime',
                   side_effect=ModelRoutingError('credential refused')):
            with pytest.raises(ModelRoutingError, match='credential refused'):
                _build('z-anthropic-deep', session_db=db)
        assert state.peek('pin', 'deep') == before
        assert db.get_meta(PREFIX + 'pin') is not None
        assert _build('z-anthropic-deep', session_db=db).model == 'z-anthropic-deep'
        assert db.get_meta(PREFIX + 'pin') is None
    finally:
        db.close()
