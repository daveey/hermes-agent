"""File/live convergence at real release and housekeeping boundaries."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from gateway import run, status
from gateway.run_agent_cache import GatewayAgentCacheMixin
from gateway.run_shutdown import GatewayShutdownMixin


class CountRunner(GatewayShutdownMixin, GatewayAgentCacheMixin):
    """Keep the real aggregate and publisher, without unrelated gateway startup."""
    def _running_agent_count(self):
        return 0

    def _running_agent_items(self):
        return []

    def _active_cron_job_count(self):
        return 0

    def _active_api_run_count(self):
        return 0

    def _describe_parked_work(self):
        return []


@pytest.fixture
def counter(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('gateway.run_shutdown.managed_turn_count', lambda runner: 0)
    runner = CountRunner()
    def read():
        assert status.flush_runtime_status(timeout=2)
        return json.loads((tmp_path / 'gateway_state.json').read_text())
    return runner, read


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['result', 'exception', 'cancel'])
async def test_deferred_worker_completion_republishes_live_count(counter, outcome):
    runner, read = counter
    future = asyncio.get_running_loop().create_future()
    runner._track_deferred_agent_worker(future, object())
    runner._persist_active_agents()  # the originating turn ends while its worker is live
    assert read()['active_agents'] == runner._active_work_count() == 1
    if outcome == 'exception':
        future.set_exception(RuntimeError('worker failed'))
    elif outcome == 'cancel':
        future.cancel()
    else:
        future.set_result(None)
    await asyncio.sleep(0)  # dispatch the real done callback, no heartbeat to mask the leak
    assert runner._active_work_count() == 0
    assert read()['active_agents'] == 0
    assert runner._deferred_agent_workers == {}


def heartbeat_from_housekeeping(monkeypatch, runner, loop):
    callbacks = []
    stop = threading.Event()

    def chore(label, fn):
        if label == 'Runtime heartbeat':
            callbacks.append(fn)
            stop.set()
    monkeypatch.setattr(run, '_housekeeping_chore', chore)
    run._start_gateway_housekeeping(stop, loop=loop, runner=runner, interval=0)
    assert len(callbacks) == 1
    return callbacks[0]


@pytest.mark.asyncio
async def test_heartbeat_converges_on_loop_not_housekeeping_thread(counter, monkeypatch):
    runner, read = counter
    status.publish_runtime_status(active_agents=4, gateway_state='running')
    loop = asyncio.get_running_loop()
    owner = threading.get_ident()
    real_count = runner._active_work_count
    calls = []

    def count():
        calls.append(threading.get_ident())
        assert threading.get_ident() == owner
        return real_count()
    runner._active_work_count = count
    tick = heartbeat_from_housekeeping(monkeypatch, runner, loop)
    await asyncio.to_thread(tick)
    await asyncio.sleep(0)
    assert calls == [owner]
    assert read()['active_agents'] == 0
    assert read()['gateway_state'] == 'running'


def test_wedged_loop_keeps_thread_stamp_and_coalesces(counter, monkeypatch):
    runner, read = counter
    pending = []
    loop = SimpleNamespace(call_soon_threadsafe=lambda cb: pending.append(cb))
    tick = heartbeat_from_housekeeping(monkeypatch, runner, loop)
    status.publish_runtime_status(active_agents=3)
    stamps = []
    for _ in range(3):
        tick()
        stamps.append(read()['updated_at'])
        assert read()['active_agents'] == 3
    assert stamps[0] < stamps[1] < stamps[2]
    assert len(pending) == 1
    pending.pop()()
    assert read()['active_agents'] == 0
    tick()
    assert len(pending) == 1  # completion re-arms the next tick


@pytest.mark.parametrize('missing', ['runner', 'loop'])
def test_heartbeat_without_loop_or_runner_still_stamps(counter, missing):
    from gateway.run_runtime_heartbeat import runtime_heartbeat
    runner, read = counter
    pending = []
    loop = SimpleNamespace(call_soon_threadsafe=pending.append)
    tick = runtime_heartbeat(None if missing == 'runner' else runner,
                             None if missing == 'loop' else loop)
    status.publish_runtime_status(active_agents=2)
    before = read()['updated_at']
    tick()
    assert read()['updated_at'] > before
    assert read()['active_agents'] == 2
    assert pending == []


def test_closed_loop_and_failed_refresh_rearm(counter):
    from gateway.run_runtime_heartbeat import runtime_heartbeat
    runner, read = counter
    pending = []
    def closed(callback):
        raise RuntimeError('closed')
    loop = SimpleNamespace(call_soon_threadsafe=closed)
    tick = runtime_heartbeat(runner, loop)
    tick()
    assert read()['updated_at']
    loop.call_soon_threadsafe = pending.append
    tick()
    assert len(pending) == 1
    original = runner._persist_active_agents
    def fail():
        raise ValueError('publication failed')
    runner._persist_active_agents = fail
    with pytest.raises(ValueError, match='publication failed'):
        pending.pop()()
    runner._persist_active_agents = original
    tick()
    assert len(pending) == 1
    pending.pop()()
    assert read()['active_agents'] == 0
