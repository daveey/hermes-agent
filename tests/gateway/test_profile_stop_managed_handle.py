"""Profile shutdown must not confuse adopted process handles with in-process agents."""
import asyncio
import queue
import threading
from types import SimpleNamespace

import pytest

from gateway.run_runtime import stop_authority_turns
from gateway.session_managed_worker import ManagedWorker


def worker():
    handle = ManagedWorker.__new__(ManagedWorker)
    handle.closed = threading.Event()
    handle.stop = asyncio.Event()
    handle.stopped_at = None
    handle.stop_cause = None
    handle.commands = queue.Queue()
    return handle


def authority(handle, registered=False, generation=4):
    return SimpleNamespace(
        _managed_workers={'s': handle} if registered else {}, _managed_spawning={},
        sessions={'s': SimpleNamespace(route='route', event_stream=SimpleNamespace(
            execution={'execution_generation': 4}))},
        adopted={'s': (generation, handle)}, pending_stops={},
        runner=SimpleNamespace(_running_agents={}),
    )


@pytest.mark.parametrize('registered', [True, False])
def test_live_managed_handle_gets_one_stop(registered):
    handle = worker()
    owner = authority(handle, registered)
    assert stop_authority_turns(owner, in_process=True, reason='Gateway restarting') == 1
    assert handle.commands.get_nowait() == {'type': 'stop'}
    assert handle.stop.is_set()
    assert handle.stop_cause == 'Gateway restarting'
    assert stop_authority_turns(owner, in_process=True, reason='Gateway shutting down') == 0
    assert handle.commands.empty()
    assert handle.stop_cause == 'Gateway restarting'
    assert not owner.pending_stops


@pytest.mark.parametrize('state', ['closed', 'stop'])
def test_retained_settling_handle_is_not_stopped_again(state):
    handle = worker()
    getattr(handle, state).set()
    assert stop_authority_turns(authority(handle), in_process=True) == 0
    assert handle.commands.empty()


def test_old_adoption_does_not_stop_old_worker():
    handle = worker()
    owner = authority(handle, generation=3)
    assert stop_authority_turns(owner, in_process=True) == 1
    assert owner.pending_stops == {'s': 4}
    assert handle.commands.empty()


def test_current_ordinary_agent_keeps_hard_interrupt_protocol():
    calls = []

    class Agent:
        def hard_interrupt(self, message, *, tool_reason):
            calls.append((message, tool_reason))

    assert stop_authority_turns(authority(Agent()), in_process=True) == 1
    assert calls == [('Profile stopping', 'gateway shutdown')]


def test_popped_worker_queue_backpressure_still_latches_stop():
    handle = worker()
    handle.commands = queue.Queue(maxsize=1)
    handle.commands.put_nowait({'type': 'other'})
    assert stop_authority_turns(authority(handle), in_process=True) == 1
    assert handle.stop.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('closed', [False, True])
async def test_profile_stop_caller_joins_and_clears_adoption(tmp_path, monkeypatch, closed):
    from gateway import session_finite
    from gateway.session_runtime_workers import join_authority_work, stop_authority_work
    from tests.gateway.test_session_authority_cancel_settlement import (
        _authority, _submit, _wire_turn_agent,
    )

    db, owner = _authority(tmp_path, monkeypatch)
    handle = worker()
    adopted = asyncio.Event()
    release = asyncio.Event()

    async def execute(authority, ref, row):
        _wire_turn_agent(authority, row['generation'], handle)
        authority._managed_workers = {'s': handle}
        authority._managed_workers.pop('s')
        if closed:
            handle.closed.set()
        adopted.set()
        await release.wait()
        return 'done'

    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    with db:
        await _submit(owner, 'managed-retire')
        await asyncio.wait_for(adopted.wait(), 5)
        try:
            stop_authority_work(owner)
            assert handle.stop.is_set() is not closed
        finally:
            release.set()
            await asyncio.wait_for(owner.sessions['s'].task, 5)
        await join_authority_work(owner, 5)
        assert not owner.adopted
        assert not owner._managed_workers
