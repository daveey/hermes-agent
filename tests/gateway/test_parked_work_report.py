"""Parked update checkpoints are reported apart from active work, and scheduled again at startup."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from gateway.run import GatewayRunner
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_runtime_workers import parked_work_units, uncounted_runtime_work
from gateway.status import flush_runtime_status
from hermes_state_runtime import begin_runtime_epoch, get_session_admission, recover_session_inputs
from hermes_state_update_pause import get_session_checkpoint
from tests.gateway.test_authority_update_pause import parked_fixture, tool_boundary, until
from tests.gateway.test_session_authority_cancel_settlement import REF, _authority, _submit


def runner_for(*authorities):
    runner = object.__new__(GatewayRunner)
    runner._restart_requested = True
    runner.adapters = {}
    runner._running_agents = {}
    runner.session_authorities = list(authorities)
    return runner


def test_parked_session_is_reported_but_not_counted_while_other_work_is(tmp_path, monkeypatch):
    db, owner, row = parked_fixture(tmp_path, monkeypatch)
    owner.update_parked['s'] = get_session_checkpoint(db, session_id='s')
    runner = runner_for(owner)
    with db:
        assert runner._describe_parked_work() == [
            {'kind': 'update_parked', 'session': 's', 'admission_id': row['admission_id'],
             'profile': owner.profile_id}]
        assert runner._describe_active_work() == []
        assert runner._active_work_count() == 0  # the updater sees no other work
        owner._side_agents = {threading.Thread(): [None]}  # unrelated work stays counted
        assert uncounted_runtime_work(runner) == 1
        assert runner._active_work_count() == 1 and len(runner._describe_active_work()) == 1
        assert len(runner._describe_parked_work()) == 1


def test_gateway_state_publishes_parked_work_and_clears_it(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db, owner, row = parked_fixture(tmp_path, monkeypatch)
    owner.update_parked['s'] = get_session_checkpoint(db, session_id='s')
    runner = runner_for(owner)
    state = tmp_path / 'gateway_state.json'
    with db:
        runner._persist_active_agents()
        assert flush_runtime_status(timeout=30.0)
        record = json.loads(state.read_text())
        assert record['parked_work'][0]['admission_id'] == row['admission_id']
        assert record['active_agents'] == 0 and not record.get('active_work')
        owner.update_parked.clear()
        runner._persist_active_agents()
        assert flush_runtime_status(timeout=30.0)
        assert json.loads(state.read_text())['parked_work'] is None


def test_active_work_verb_lists_parked_apart_and_scopes_by_profile(tmp_path, monkeypatch):
    from gateway.run_active_work_verb import active_work_verb
    db, owner, _row = parked_fixture(tmp_path, monkeypatch)
    owner.update_parked['s'] = get_session_checkpoint(db, session_id='s')
    runner = runner_for(owner)
    runner._served_profile_homes = {}
    loop = SimpleNamespace(call_soon_threadsafe=lambda fn: fn())
    with db:
        handler = active_work_verb(runner, loop)
        whole = handler({})
        assert whole['active_agents'] == 0 and whole['active_work'] == []
        assert [u['session'] for u in whole['parked_work']] == ['s']
        scoped = handler({'profile': owner.profile_id})
        assert scoped['active_work'] == [] and len(scoped['parked_work']) == 1
        assert handler({'profile': 'someone-else'})['parked_work'] == []


@pytest.mark.asyncio
async def test_real_park_publishes_then_resume_and_stop_clear_report(tmp_path, monkeypatch):
    from gateway import session_finite
    from hermes_cli import admission_fence
    db, owner = _authority(tmp_path, monkeypatch)
    held = [True]
    monkeypatch.setattr(admission_fence, 'is_held', lambda root: held[0])
    calls = []

    async def execute(authority, ref, row):
        calls.append(row)
        if len(calls) == 1:
            tool_boundary(db)
            authority.pending_results[row['admission_id']] = {'result': {'paused_for_update': True}, 'usage': {}}
            return None
        return 'resumed'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    runner = runner_for(owner)
    with db:
        head = await _submit(owner, 'original')
        try:
            await until(lambda: parked_work_units(runner))
            assert parked_work_units(runner)[0]['admission_id'] == head.admission_id
            assert runner._active_work_count() == 0
            held[0] = False
            await asyncio.wait_for(owner.sessions['s'].task, 5)
            assert parked_work_units(runner) == []
        finally:
            task = owner.sessions['s'].task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_restart_schedules_recovered_checkpoint_held_until_pause_clears(tmp_path, monkeypatch):
    from gateway import session_finite
    from gateway.session_local_recovery import recover_local_sessions
    from hermes_cli import admission_fence
    db, old, row = parked_fixture(tmp_path, monkeypatch)
    held = [True]
    monkeypatch.setattr(admission_fence, 'is_held', lambda root: held[0])
    calls = []

    async def execute(authority, ref, claimed):
        calls.append(claimed)
        return 'resumed'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    with db:
        epoch = begin_runtime_epoch(db, instance_id='replacement')
        recover_session_inputs(db, epoch=epoch)
        assert get_session_checkpoint(db, session_id='s') is not None  # valid receipt survives recovery
        owner = SessionAuthority(old.runner, profile_id='owned', instance_id='replacement', db=db, epoch=epoch)
        monkeypatch.setattr('gateway.session_local_recovery.local_receipt_ids', lambda authority: ['s'])
        monkeypatch.setattr('gateway.session_local_recovery.restore_local_session',
                            lambda authority, sid: _restored(owner, old))
        recover_local_sessions(owner, schedule=True)
        try:
            await until(lambda: owner.update_parked.get('s'))
            assert not calls and not owner.executing
            assert parked_work_units(runner_for(owner))[0]['admission_id'] == row['admission_id']
            held[0] = False
            await asyncio.wait_for(owner.sessions['s'].task, 5)
            assert [c['request_id'] for c in calls] == ['original']
            assert calls[0]['update_resume']['admission_id'] == row['admission_id']
            assert get_session_admission(db, admission_id=row['admission_id'])['status'] == 'terminal'
        finally:
            task = owner.sessions['s'].task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def _restored(owner, old):
    owner.sessions['s'] = LiveSession(old.sessions['s'].source, 'route')
    return REF
