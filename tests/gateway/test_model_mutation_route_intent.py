"""The canonical model mutation writes its route-switch intent only when the switch commits. [daveey]

Only a committed receipt may later move an allocation (``agent.model_routing_switch_intent``): a refused
(optimistic revision conflict) or deferred preparation must leave allocation state and intents alone, and
preparation's own route lookups must never allocate in the durable store.
"""
import asyncio
import json
import threading

import pytest

from agent.model_routing_switch_intent import PREFIX
from tests.gateway.test_session_mutation_model_busy import _mutate, _owner, _running_model
from tests.waiting import async_wait_until


def _intent(db, sid):
    raw = db.get_meta(PREFIX + sid)
    return json.loads(raw) if raw else None


def _seed_stale_route(db, sid):
    db.patch_session_model_config(sid, {'model_routing': {
        'requested_class': 'deep', 'selected_model': 'frozen', 'selected_provider': 'anthropic',
        'billing_group': 'subscription', 'exact_pin': True}})


def _routing(db, sid):
    config = db.get_session(sid)['model_config']
    config = json.loads(config) if isinstance(config, str) else (config or {})
    return config.get('model_routing')


async def _noop(event):
    return 'ok'


@pytest.mark.asyncio
async def test_committed_pick_records_the_intent_and_drops_the_stale_record(tmp_path, monkeypatch):
    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, _noop)
    try:
        _seed_stale_route(db, ref.session_id)
        reply = await _mutate(owner, db, ref, 'pick', 'picked')
        assert reply['result']['model'] == 'picked', reply
        row = db.get_session(ref.session_id)
        assert row['model'] == 'picked'
        assert _routing(db, ref.session_id) is None  # restore_route no longer resumes the old pin
        intent = _intent(db, ref.session_id)
        assert (intent['session_id'], intent['model'], intent['provider']) == (ref.session_id, 'picked', 'custom')
        assert intent['generation'] == row['runtime_generation']
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_optimistic_rejection_changes_no_route_state(tmp_path, monkeypatch):
    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, _noop)
    try:
        _seed_stale_route(db, ref.session_id)
        before = _routing(db, ref.session_id)
        snap = dict(db.get_session(ref.session_id))
        snap['runtime_revision'] += 7
        reply = await _mutate(owner, db, ref, 'stale', 'picked', snap)
        assert reply['error']['data']['reason'] == 'revision_conflict'
        assert _intent(db, ref.session_id) is None
        assert _routing(db, ref.session_id) == before
        assert db.get_session(ref.session_id)['model'] == 'frozen'
        assert not (tmp_path / 'model-routing.sqlite3').exists()
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_deferred_pick_writes_its_intent_only_at_the_turn_boundary(tmp_path, monkeypatch):
    from gateway.session_contract import Submission
    import hermes_state_runtime as rt
    started, release = threading.Event(), asyncio.Event()
    state = {}

    async def handle(event):
        if event.text == 'RUNNING':
            started.set()
            await release.wait()
        return 'ok'
    store, db, authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    state.update(authority=authority, ref=ref)
    try:
        _seed_stale_route(db, ref.session_id)
        before = _routing(db, ref.session_id)
        first = await authority.submit(owner.actor, Submission('running', ref, {'text': 'RUNNING'}, 'queue'))
        await async_wait_until(started.is_set, timeout=10, what='the first turn is running')
        reply = await _mutate(owner, db, ref, 'pick', 'picked')
        assert reply['result']['status'] == 'deferred', reply
        assert _intent(db, ref.session_id) is None
        assert _routing(db, ref.session_id) == before
        release.set()
        await async_wait_until(
            lambda: rt.get_session_admission(db, admission_id=first.admission_id)['status'] == 'terminal',
            timeout=10, what='the running turn settled')
        await async_wait_until(lambda: _running_model(authority, ref) == 'picked',
                               timeout=10, what='the pick published at the turn boundary')
        assert _intent(db, ref.session_id)['model'] == 'picked'
        assert _routing(db, ref.session_id) is None
    finally:
        release.set()
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_preparation_route_lookups_never_allocate(tmp_path, monkeypatch):
    """The current-route lookup and the target resolution run as temporary (in-memory) resolutions."""
    from agent.model_routing_runtime import _temporary_resolution
    from hermes_cli import model_switch
    seen = []
    store, db, authority, owner, ref = _owner(tmp_path, monkeypatch, _noop)
    real_switch = model_switch.switch_model

    def lookup(**kwargs):
        seen.append(('current', _temporary_resolution.get()))
        return 'frozen', {}

    def switch(**kwargs):
        seen.append(('target', _temporary_resolution.get()))
        return real_switch(**kwargs)
    authority.runner._resolve_session_agent_runtime = lookup
    monkeypatch.setattr(model_switch, 'switch_model', switch)
    try:
        reply = await _mutate(owner, db, ref, 'pick', 'picked')
        assert reply['result']['model'] == 'picked', reply
        assert ('current', True) in seen and ('target', True) in seen
        assert all(flag for _, flag in seen)
        assert not (tmp_path / 'model-routing.sqlite3').exists()
    finally:
        await owner.close()
        store.close_all_db_handles()
