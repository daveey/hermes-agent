"""Behavior guards for update-pause bookkeeping that earlier suites left unobserved."""
from tests.hermes_state.raw_rows import raw_append_message
import types

import pytest

from gateway import session_update_pause as sup
from gateway.session_update_pause_hook import installed, bind_scope, is_paused
from hermes_state_runtime import get_session_admission
from hermes_state_update_pause import (
    CHECKPOINT_PREFIX, delete_session_checkpoint, get_session_checkpoint, recover_checkpointed_inputs,
)
from tests.gateway.test_authority_update_pause import parked_fixture
from tests.gateway.test_session_authority_cancel_settlement import REF


def meta(db, key):
    with db._read_ctx() as conn:
        return conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()


def test_delete_checkpoint_removes_only_its_own_receipt(tmp_path, monkeypatch):
    db, _owner, head = parked_fixture(tmp_path, monkeypatch)
    with db:
        db._execute_write(lambda c: c.execute("INSERT INTO state_meta(key,value) VALUES(?,?)",
                                              (CHECKPOINT_PREFIX + 'other', 'keep')))
        db._execute_write(lambda c: delete_session_checkpoint(c, head['admission_id']))
        assert meta(db, CHECKPOINT_PREFIX + head['admission_id']) is None
        assert meta(db, CHECKPOINT_PREFIX + 'other') is not None


def test_recovery_preserves_valid_receipt_and_unknowns_invalid_one(tmp_path, monkeypatch):
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    with db:
        # A different epoch is a replacement owner: a valid receipt stays resumable.
        assert db._execute_write(lambda c: recover_checkpointed_inputs(c, owner.epoch + 1)) == 0
        assert get_session_admission(db, admission_id=head['admission_id'])['status'] == 'started'
        assert get_session_checkpoint(db, session_id='s')['admission_id'] == head['admission_id']
        # Same-epoch rows are never touched.
        assert db._execute_write(lambda c: recover_checkpointed_inputs(c, owner.epoch)) == 0
        # Corrupt the proof: recovery fences the head and removes the dead receipt.
        raw_append_message(db, 's', 'assistant', 'tail changed')
        # The current owner's own started row is live work even when its proof is stale.
        assert db._execute_write(lambda c: recover_checkpointed_inputs(c, owner.epoch)) == 0
        assert get_session_admission(db, admission_id=head['admission_id'])['status'] == 'started'
        assert db._execute_write(lambda c: recover_checkpointed_inputs(c, owner.epoch + 1)) == 1
        assert get_session_admission(db, admission_id=head['admission_id'])['status'] == 'unknown'
        assert meta(db, CHECKPOINT_PREFIX + head['admission_id']) is None


@pytest.mark.asyncio
async def test_externally_settled_parked_head_drops_cached_hint(tmp_path, monkeypatch):
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    with db:
        assert sup.checkpoint_head(owner, REF)['admission_id'] == head['admission_id']
        assert 's' in owner.update_parked
        db._execute_write(lambda c: c.execute(
            "UPDATE session_admissions SET status='terminal', outcome='interrupted' WHERE admission_id=?",
            (head['admission_id'],)))
        assert sup.checkpoint_head(owner, REF) is None
        assert 's' not in owner.update_parked


@pytest.mark.asyncio
async def test_vanished_proof_for_cached_started_head_fails_closed(tmp_path, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    with db:
        sup.checkpoint_head(owner, REF)
        db._execute_write(lambda c: delete_session_checkpoint(c, head['admission_id']))
        with pytest.raises(RuntimeStoreError, match='invalid_checkpoint'):
            sup.checkpoint_head(owner, REF)


@pytest.mark.asyncio
async def test_uncached_invalid_proof_raises_not_none(tmp_path, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    db, owner, _head = parked_fixture(tmp_path, monkeypatch)
    with db:
        raw_append_message(db, 's', 'assistant', 'tail changed')
        with pytest.raises(RuntimeStoreError, match='invalid_checkpoint'):
            sup.checkpoint_head(owner, REF)


def row(admission='a', generation=1, **extra):
    return {'admission_id': admission, 'generation': generation, 'target_session_id': 's', **extra}


def test_ordinary_claim_discards_previous_stop_latch_but_resume_keeps_it():
    authority = types.SimpleNamespace(update_claims={}, update_stops=set())
    sup.remember_claim(authority, 's', row('old', 1))
    authority.update_stops.add('old')
    sup.claim_started(authority, 's', row('new', 1))
    assert 'old' not in authority.update_stops and 's' not in authority.update_claims
    sup.remember_claim(authority, 's', row('old', 1))
    authority.update_stops.add('old')
    sup.claim_started(authority, 's', row('old', 2, update_resume={'generation': 1}))
    assert 'old' in authority.update_stops
    assert authority.update_claims['s'] == ('old', {1, 2})


def test_remembering_a_different_admission_resets_generations_and_stop():
    authority = types.SimpleNamespace(update_claims={}, update_stops=set())
    sup.remember_claim(authority, 's', row('old', 1))
    authority.update_stops.add('old')
    sup.remember_claim(authority, 's', row('new', 5))
    assert authority.update_claims['s'] == ('new', {5})
    assert 'old' not in authority.update_stops


def test_stopped_checks_each_latch_independently():
    base = row('a', 3)
    for latch in ('update_stops', 'pending_stops', 'delivered_stops'):
        authority = types.SimpleNamespace(update_stops=set(), pending_stops={}, delivered_stops={})
        assert not sup.stopped(authority, base)
        if latch == 'update_stops':
            authority.update_stops.add('a')
        else:
            getattr(authority, latch)['s'] = 3
        assert sup.stopped(authority, base), latch
    authority = types.SimpleNamespace(update_stops=set(), pending_stops={'s': 2}, delivered_stops={'s': 4})
    assert not sup.stopped(authority, base)


@pytest.mark.asyncio
async def test_old_generation_stop_cannot_hit_fresh_successor(tmp_path, monkeypatch):
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    with db:
        sup.remember_claim(owner, 's', head)
        owner.sessions['s'].event_stream.execution = {'admission_id': 'someone-else'}
        assert await sup.interrupt_update(owner, REF, head['generation']) is False
        assert head['admission_id'] not in owner.update_stops
        assert get_session_admission(db, admission_id=head['admission_id'])['status'] == 'started'
        # Unknown generation is also refused even with a matching execution.
        owner.sessions['s'].event_stream.execution = {'admission_id': head['admission_id']}
        assert await sup.interrupt_update(owner, REF, head['generation'] + 9) is False


def test_fresh_owner_remembers_original_resume_generation():
    authority = types.SimpleNamespace(update_claims={}, update_stops=set())
    sup.claim_started(authority, 's', row('a', 8, update_resume={'generation': 3}))
    assert authority.update_claims['s'] == ('a', {3, 8})


@pytest.mark.asyncio
async def test_terminal_checkpoint_cannot_latch_stop(tmp_path, monkeypatch):
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    with db:
        sup.remember_claim(owner, 's', head)
        db._execute_write(lambda c: c.execute(
            "UPDATE session_admissions SET status='terminal' WHERE admission_id=?", (head['admission_id'],)))
        assert await sup.interrupt_update(owner, REF, head['generation']) is False
        assert not owner.update_stops


@pytest.mark.asyncio
async def test_refusal_clears_receipt_cache_and_publishes_revision(tmp_path, monkeypatch):
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    with db:
        sup.checkpoint_head(owner, REF)
        with db._read_ctx() as conn:
            revision = conn.execute("SELECT runtime_revision FROM sessions WHERE id='s'").fetchone()[0]
        assert await sup.refuse_checkpoint(owner, REF, owner.sessions['s'], head) is True
        assert meta(db, CHECKPOINT_PREFIX + head['admission_id']) is None
        assert 's' not in owner.update_parked
        with db._read_ctx() as conn:
            assert conn.execute("SELECT runtime_revision FROM sessions WHERE id='s'").fetchone()[0] > revision


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['before', 'after', 'none'])
async def test_park_stop_fences_and_publication_at_boundary(tmp_path, monkeypatch, phase, caplog):
    import logging
    caplog.set_level(logging.INFO, logger=sup.__name__)
    from gateway import session_runtime_workers
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    calls = []
    original = sup.checkpoint_session_input

    def checkpoint(*args, **kwargs):
        calls.append('checkpoint')
        receipt = original(*args, **kwargs)
        if phase == 'after':
            owner.update_stops.add(head['admission_id'])
        return receipt

    async def settle(*args):
        calls.append('settle')
        return False

    monkeypatch.setattr(sup, 'checkpoint_session_input', checkpoint)
    monkeypatch.setattr(sup, 'settle_interrupted', settle)
    monkeypatch.setattr(owner, '_publish_pending', lambda ref: calls.append('publish'))
    monkeypatch.setattr(session_runtime_workers, 'persist_work_count', lambda authority: calls.append('count'))
    with db:
        if phase == 'before':
            owner.update_stops.add(head['admission_id'])
        result = await sup.park_or_interrupt(owner, owner.sessions['s'], REF, head)
        if phase == 'none':
            assert result is True and calls == ['checkpoint', 'count', 'publish', 'count']
            assert f"Parked gateway turn for update: session=s admission={head['admission_id']}" in caplog.text
        else:
            assert 'Parked gateway turn for update:' not in caplog.text
            assert result is False and calls == (['settle'] if phase == 'before' else ['checkpoint', 'count', 'settle'])
            assert 's' not in owner.update_parked


@pytest.mark.asyncio
async def test_interrupted_settlement_drops_pause_and_old_delivery_before_delivery(tmp_path, monkeypatch):
    from gateway import session_settlement_recovery
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    reached = []

    async def settle(authority, live, ref, current, text, outcome):
        assert 's' not in authority.update_parked
        assert head['admission_id'] not in authority.pending_deliveries
        assert outcome == 'interrupted'
        reached.append(True)
        return True

    monkeypatch.setattr(session_settlement_recovery, 'settle_and_deliver', settle)
    with db:
        sup.checkpoint_head(owner, REF)
        owner.pending_deliveries[head['admission_id']] = 'stale'
        assert await sup.settle_interrupted(owner, owner.sessions['s'], REF, head)
        assert reached == [True]


def agent_with(**attrs):
    return types.SimpleNamespace(**attrs)


def test_hook_restores_prior_hook_or_removes_when_absent(tmp_path):
    marker = lambda: None
    with bind_scope(tmp_path, eligible=True, message_id='m'):
        cached = agent_with(step_boundary_pause=marker)
        with installed(cached, message_id='m') as active:
            assert active and cached.step_boundary_pause is not marker
        assert cached.step_boundary_pause is marker
        fresh = agent_with()
        with installed(fresh, message_id='m'):
            assert hasattr(fresh, 'step_boundary_pause')
        assert not hasattr(fresh, 'step_boundary_pause')
        raising = agent_with(step_boundary_pause=marker)
        with pytest.raises(RuntimeError):
            with installed(raising, message_id='m'):
                raise RuntimeError
        assert raising.step_boundary_pause is marker


def test_ineligible_scope_never_installs(tmp_path):
    with bind_scope(tmp_path, eligible=False, message_id='m'):
        agent = agent_with()
        with installed(agent, message_id='m') as active:
            assert active is False and not hasattr(agent, 'step_boundary_pause')


def test_is_paused_rejects_foreign_truthy_reasons():
    assert is_paused({'paused_for_update': True})
    assert not is_paused({'paused_for_update': 'something-else'})
    assert not is_paused({'paused_for_update': 1.0 * 0}) and not is_paused(None) and not is_paused('x')


def test_paused_result_projects_flag_and_finish_consumer_skips_seal():
    from gateway.run_turn_runner import TurnRunner, project_paused_result
    assert project_paused_result({'final_response': 'x'}, {'k': 1}) is None
    assert project_paused_result({'paused_for_update': True, 'final_response': 'x'}, {'k': 1}) == {
        'final_response': '', 'paused_for_update': True, 'k': 1}
    sealed = []
    consumer = types.SimpleNamespace(finish=lambda *a, **k: sealed.append(1), __getattr__=lambda s, n: None)
    holder = [None]
    runner = types.SimpleNamespace(_ctx=types.SimpleNamespace(result_holder=holder))
    result = {'paused_for_update': True, 'final_response': 'partial'}
    TurnRunner._finish_stream_consumer(runner, result, [], consumer)
    assert holder[0] is result and not sealed and result['final_response'] == 'partial'
