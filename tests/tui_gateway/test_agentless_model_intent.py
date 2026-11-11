"""Failed-build picks cross the TUI setter, durable intent and real allocator."""
import json
import threading
from contextlib import nullcontext

import pytest

from agent.model_routing_state import RoutingState
from hermes_cli.model_switch import ModelSwitchResult
from hermes_state import SessionDB
from tests.agent import test_route_switch_intent as routes
from tests.tui_gateway.test_failed_agent_build_retry import failed_session
from tui_gateway import server

managed = routes.managed
routing_home = routes.routing_home
streaming_route = routes.streaming_route
destinations = routes.destinations
seams = routes.seams


@pytest.fixture
def picking(destinations, seams, tmp_path, monkeypatch):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('pin', 'desktop', model='anthropic-deep')
    session = failed_session(tmp_path, 'ui-id')
    session['session_key'] = 'pin'  # UI id is NOT the allocation/transcript id.
    session['resume_runtime_overrides'] = {
        'model_override': {'model': 'anthropic-deep', 'provider': 'anthropic'},
        'provider_override': 'anthropic', 'keep': 'resume-history'}
    monkeypatch.setattr(server, '_session_profile_runtime_scope', lambda *a: nullcontext())
    monkeypatch.setattr(server, '_current_model_runtime', lambda *a: ('anthropic', 'anthropic-deep', '', ''))
    def preflight(model, provider, *a, **kw):
        result = ModelSwitchResult(success=True, new_model=model, target_provider=provider or 'anthropic')
        return result, result, {}, None
    monkeypatch.setattr(server, '_preflight_model_switch', preflight)
    monkeypatch.setattr(server, '_expensive_model_confirm', lambda *a: None)
    monkeypatch.setattr(server, '_persist_live_session_runtime', lambda *a: None)
    monkeypatch.setattr('hermes_cli.observability.shared_metrics_events.record_model_switch', lambda **kw: None)
    yield db, session
    server._sessions.pop('ui-id', None)
    db.close()


def test_failed_build_pick_rebuilds_new_exact_route(picking, tmp_path, monkeypatch):
    db, session = picking
    routes._build('anthropic-deep', session_db=db)
    state = RoutingState(tmp_path / 'model-routing.sqlite3')
    assert state.peek('pin', 'deep')['exact_pin']
    def rebuild(sid, current):
        kwargs = current['resume_runtime_overrides']
        assert kwargs['keep'] == 'resume-history'
        model = kwargs['model_override']['model']
        try:
            current['agent'] = routes._build(model, session_db=db)
        except Exception as exc:
            current['agent_error'] = str(exc)
        finally:
            current['agent_ready'].set()
    monkeypatch.setattr(server, '_start_agent_build', rebuild)
    result = server._set_model(1, {'session_id': 'ui-id', 'confirm_expensive_model': True},
                               'model', 'z-anthropic-deep --provider anthropic --session', session)
    assert 'error' not in result, result
    assert session['agent'].model == 'z-anthropic-deep'
    row = state.peek('pin', 'deep')
    assert (row['selected_model'], row['reason'], row['pin_source']) == (
        'z-anthropic-deep', 'explicit-switch', 'explicit')
    assert db.route_switch_intent('pin') is None
    assert db.route_switch_intent('ui-id') is None


def test_later_pick_supersedes_previous_tuple(picking):
    db, session = picking
    def raw(model):
        return f'{model} --provider anthropic --session'
    server._apply_model_switch('ui-id', session, raw('z-anthropic-deep'))
    first = json.loads(db.route_switch_intent('pin'))
    server._apply_model_switch('ui-id', session, raw('anthropic-standard'))
    second = json.loads(db.route_switch_intent('pin'))
    assert second['model'] == 'anthropic-standard'
    assert second['event']['store'] == first['event']['store']
    assert second['event']['seq'] > first['event']['seq']
    assert session['model_override']['model'] == second['model']


def test_unconfirmed_pick_records_nothing(picking, monkeypatch):
    db, session = picking
    monkeypatch.setattr(server, '_expensive_model_confirm', lambda *a: {'confirm_required': True})
    result = server._apply_model_switch('ui-id', session, 'z-anthropic-deep --provider anthropic --session')
    assert result['confirm_required']
    assert db.route_switch_intent('pin') is None
    assert 'model_override' not in session


def test_intent_write_failure_does_not_publish_override(picking, monkeypatch):
    db, session = picking
    def refused(*a):
        raise RuntimeError('intent write refused')
    monkeypatch.setattr('agent.model_routing_switch_intent.record_in_transaction', refused)
    with pytest.raises(RuntimeError, match='intent write refused'):
        server._apply_model_switch('ui-id', session, 'z-anthropic-deep --provider anthropic --session')
    assert 'model_override' not in session
    assert db.route_switch_intent('pin') is None


@pytest.mark.parametrize('case', ['no-key', 'internal', 'once', 'unavailable-db'])
def test_non_commits_and_unavailable_store(picking, monkeypatch, case):
    db, session = picking
    options = {}
    raw = 'z-anthropic-deep --provider anthropic --session'
    if case == 'no-key':
        session.pop('session_key')
    elif case == 'internal':
        options['pin_session_override'] = False
    elif case == 'once':
        raw = 'z-anthropic-deep --provider anthropic --once'
    else:
        monkeypatch.setattr(server, '_session_db', lambda *a: nullcontext(None))
    if case in ('once', 'unavailable-db'):
        with pytest.raises(ValueError):
            server._apply_model_switch('ui-id', session, raw, **options)
        assert 'model_override' not in session
    else:
        server._apply_model_switch('ui-id', session, raw, **options)
    assert db.route_switch_intent('pin') is None


def test_intent_uses_stored_generation_and_allows_new_lazy_row(picking):
    db, session = picking
    db._execute_write(lambda conn: conn.execute("UPDATE sessions SET runtime_generation=7 WHERE id='pin'"))
    server._apply_model_switch('ui-id', session, 'z-anthropic-deep --provider anthropic --session')
    assert json.loads(db.route_switch_intent('pin'))['generation'] == 7
    session['session_key'] = 'fresh-unbuilt'
    server._apply_model_switch('ui-id', session, 'z-anthropic-deep --provider anthropic --session')
    assert json.loads(db.route_switch_intent('fresh-unbuilt'))['generation'] == 0


def test_concurrent_picks_publish_in_commit_order(picking, monkeypatch):
    db, session = picking
    committed = threading.Event()
    second_entering = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    errors = []
    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == 'pick-B':
                second_entering.set()
            lock.acquire()
        def __exit__(self, *args):
            lock.release()
    session['agent_build_lock'] = ObservedLock()
    monkeypatch.setattr(server, '_session_db', lambda *a: nullcontext(db))
    original = db._execute_write
    def write(fn):
        result = original(fn)
        if threading.current_thread().name == 'pick-A':
            committed.set()
            assert release.wait(5)
        return result
    monkeypatch.setattr(db, '_execute_write', write)
    def pick(model):
        try:
            server._apply_model_switch('ui-id', session, f'{model} --provider anthropic --session')
        except Exception as exc:
            errors.append(exc)
    first = threading.Thread(target=pick, args=('z-anthropic-deep',), name='pick-A')
    second = threading.Thread(target=pick, args=('anthropic-standard',), name='pick-B')
    first.start()
    try:
        assert committed.wait(5)
        second.start()
        assert second_entering.wait(5)
        assert json.loads(db.route_switch_intent('pin'))['model'] == 'z-anthropic-deep'
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    intent = json.loads(db.route_switch_intent('pin'))
    assert intent['model'] == session['model_override']['model'] == 'anthropic-standard'
