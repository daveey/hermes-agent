"""Committed switch intents replace allocations in commit order, each as its own event. [daveey]

A build reads its intent, then allocates; nothing serializes that window against a later switch. Keyed by
equality alone, a build that read intent A could allocate AFTER a newer intent B was honoured, replace B's
allocation with A's tuple, miss its compare-delete, and leave the next exact build on B refusing ("Exact
pinned route cannot fail over"). And an identical intent recorded again (same tuple and generation, as a
profile selection A -> B -> A writes) produced the same fence token as the honoured one. Every intent now
carries a store-ordered event; the allocator replaces only for an event newer than the one it honoured.
"""
import json
import threading
from unittest.mock import patch

import pytest

import agent.model_routing_switch_intent as switch_intent
from agent.model_routing import ModelRoutingError
from agent.model_routing_state import RoutingState
from agent.model_routing_switch_intent import PREFIX, fence_token, record_in_transaction, supersedes
from hermes_state import SessionDB
from tests.agent import test_route_switch_intent as base

managed = base.managed
routing_home = base.routing_home
streaming_route = base.streaming_route
destinations = base.destinations
seams = base.seams
_build = base._build


def _record(db, session, model, generation=2):
    db._execute_write(lambda conn: record_in_transaction(conn, session, model, 'anthropic', generation))
    return db.route_switch_intent(session)


def test_identical_intents_are_distinct_ordered_events(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    try:
        first = _record(db, 's', 'z-anthropic-deep')
        again = _record(db, 's', 'z-anthropic-deep')
        assert first != again
        assert json.loads(first)['event']['seq'] + 1 == json.loads(again)['event']['seq']
        assert json.loads(first)['generation'] == json.loads(again)['generation'] == 2
        older, newer = fence_token(first), fence_token(again)
        assert older != newer
        assert supersedes(newer, older) and not supersedes(older, newer)
        assert not supersedes(newer, newer)  # a retry / overlapping build of the same event
        assert supersedes(older, None)
        # The counter is per store, shared by every session in it, so order follows commit order.
        other = fence_token(_record(db, 'other', 'z-anthropic-deep'))
        assert supersedes(other, newer)
    finally:
        db.close()


def test_unordered_and_cross_store_tokens_keep_the_equality_fence(tmp_path):
    legacy = json.dumps({'session_id': 's', 'model': 'm', 'provider': 'anthropic', 'generation': 2})
    token = fence_token(legacy)
    assert token.startswith('switch-intent:') and ':v2:' not in token
    assert not supersedes(token, token)
    ordered = []
    for name in ('a.db', 'b.db'):
        db = SessionDB(tmp_path / name)
        try:
            ordered.append(fence_token(_record(db, 's', 'm')))
        finally:
            db.close()
    assert ordered[0] != ordered[1]
    assert supersedes(ordered[0], ordered[1]) and supersedes(ordered[1], ordered[0])
    assert supersedes(token, ordered[0]) and supersedes(ordered[0], token)


def test_stale_older_build_after_a_newer_switch_cannot_replace_or_wedge(destinations, tmp_path, seams):
    """Real AIAgent builds: an older intent's build that allocates late leaves the newer pin standing."""
    _build('anthropic-deep')
    routes = RoutingState(tmp_path / 'model-routing.sqlite3')
    db = SessionDB(tmp_path / 'state-under-test.db')
    read, release = threading.Event(), threading.Event()
    real = switch_intent.pending

    def pending(*args, **kwargs):
        raw = real(*args, **kwargs)
        if threading.current_thread().name == 'stale':
            read.set()
            assert release.wait(20)
        return raw
    outcome = {}

    def stale():
        try:
            outcome['agent'] = _build('z-anthropic-deep', session_db=db)
        except BaseException as exc:  # asserted below
            outcome['error'] = exc
    try:
        _record(db, 'pin', 'z-anthropic-deep')  # intent A
        with patch.object(switch_intent, 'pending', pending):
            thread = threading.Thread(target=stale, name='stale')
            thread.start()
            assert read.wait(20)  # the stale build holds intent A, not yet allocated
            _record(db, 'pin', 'anthropic-deep', generation=3)  # newer intent B supersedes A
            newer = _build('anthropic-deep', session_db=db)
            assert newer.model == 'anthropic-deep'
            assert db.get_meta(PREFIX + 'pin') is None  # B consumed
            release.set()
            thread.join(30)
        assert not thread.is_alive()
        assert isinstance(outcome.get('error'), ModelRoutingError), outcome
        assert 'cannot fail over' in str(outcome['error'])
        recorded = routes.peek('pin', 'deep')
        assert (recorded['selected_model'], recorded['exact_pin'], recorded['reason']) == (
            'anthropic-deep', True, 'explicit-switch')
        # No wedge: the next exact build on the newer pick resolves from the durable route.
        assert _build('anthropic-deep', session_db=db).model == 'anthropic-deep'
    finally:
        release.set()
        db.close()


def test_repeated_identical_intent_after_an_unfenced_switch_replaces_again(destinations, tmp_path, seams):
    """A -> in-session /model -> the same tuple and generation again: a new event, not a lingering retry."""
    _build('anthropic-deep')
    path = tmp_path / 'model-routing.sqlite3'
    routes = RoutingState(path)
    db = SessionDB(tmp_path / 'state-under-test.db')
    try:
        _record(db, 'pin', 'z-anthropic-deep')
        assert _build('z-anthropic-deep', session_db=db).model == 'z-anthropic-deep'
        honoured = routes.peek('pin', 'deep')
        route = {'provider': honoured['selected_provider'], 'model': 'anthropic-deep',
                 'billing': honoured['billing_group'], 'weight': 1}
        moved = routes.allocate('pin', None, 'deep', [route], honoured['policy_version'], (),
                                exact_pin=True, replace=True)  # unfenced in-session switch
        assert moved['selected_model'] == 'anthropic-deep'
        _record(db, 'pin', 'z-anthropic-deep')  # byte-identical apart from its event
        assert _build('z-anthropic-deep', session_db=db).model == 'z-anthropic-deep'
        assert routes.peek('pin', 'deep')['selected_model'] == 'z-anthropic-deep'
        assert db.get_meta(PREFIX + 'pin') is None
    finally:
        db.close()


def test_rollback_then_newer_intent_still_replaces_in_order(destinations, tmp_path, seams):
    """A refused credential fences nothing; a newer intent and a crash retry of it behave in order."""
    _build('anthropic-deep')
    routes = RoutingState(tmp_path / 'model-routing.sqlite3')
    db = SessionDB(tmp_path / 'state-under-test.db')
    try:
        _record(db, 'pin', 'z-anthropic-deep')
        with patch('agent.model_routing_runtime.verify_class_runtime',
                   side_effect=ModelRoutingError('credential refused')):
            with pytest.raises(ModelRoutingError, match='credential refused'):
                _build('z-anthropic-deep', session_db=db)
        assert routes.peek('pin', 'deep')['selected_model'] == 'anthropic-deep'
        with patch.object(switch_intent, 'consume', lambda *a, **k: None):  # crash before consume
            assert _build('z-anthropic-deep', session_db=db).model == 'z-anthropic-deep'
        committed = routes.peek('pin', 'deep')
        assert _build('z-anthropic-deep', session_db=db).model_routing == committed  # retry is sticky
        assert db.get_meta(PREFIX + 'pin') is None
        _record(db, 'pin', 'anthropic-deep', generation=3)
        assert _build('anthropic-deep', session_db=db).model == 'anthropic-deep'
        assert routes.peek('pin', 'deep')['selected_model'] == 'anthropic-deep'
    finally:
        db.close()
