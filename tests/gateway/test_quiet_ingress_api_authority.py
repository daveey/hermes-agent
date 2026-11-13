"""R4-F F3/F4a: api_server turn pre-reservation window and AuthorityConnection.dispatch entry scope."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from hermes_cli import local_work
from hermes_cli.local_work import Registry
from hermes_constants import hermes_home_key
from tests.waiting import wait_until


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reg = Registry(homes=[hermes_home_key(tmp_path)])
    monkeypatch.setattr(local_work, "_registry", reg)
    return reg


@pytest.fixture
def one_worker_pool(monkeypatch):
    """A saturated one-thread API turn executor: the next submission waits in the queue."""
    from gateway.platforms import api_server_runs
    pool = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(api_server_runs, "api_turn_executor", lambda: pool)
    gate, running = threading.Event(), threading.Event()
    pool.submit(lambda: (running.set(), gate.wait(10)))
    assert running.wait(5)
    yield pool, gate
    gate.set()
    pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_submit_api_worker_reserves_while_queued_and_cancel_of_queued_releases(registry, one_worker_pool):
    from gateway.platforms import api_server_runs as runs
    loop = asyncio.get_running_loop()
    before_live = runs.api_worker_live_count()
    ran = []
    waiter = runs._submit_api_worker(loop, lambda: ran.append(1))
    # Still waiting behind the occupied thread: no run_conversation yet, but the turn is already counted.
    assert registry.snapshot().active == 1
    assert not ran
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    await asyncio.to_thread(wait_until, lambda: registry.snapshot().active == 0, what="queued job cancelled -> released")
    assert runs.api_worker_live_count() == before_live  # cancelled-before-start also drops the live count
    assert "unmatched-finish" not in registry.snapshot().unknowns


@pytest.mark.asyncio
async def test_cancelled_awaiter_of_running_api_worker_keeps_reservation_until_thread_exits(registry, monkeypatch):
    from gateway.platforms import api_server_runs as runs
    pool = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(runs, "api_turn_executor", lambda: pool)
    started, gate = threading.Event(), threading.Event()

    def body():
        started.set()
        assert gate.wait(10)
        return "done"

    waiter = runs._submit_api_worker(asyncio.get_running_loop(), body)
    await asyncio.to_thread(started.wait, 5)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert registry.snapshot().active == 1  # the thread still runs: custody stays with it
    assert runs.api_worker_live_count() >= 1
    gate.set()
    wait_until(lambda: registry.snapshot().active == 0, what="thread exit release")
    pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_on_turn_executor_cron_fire_path_reserves_before_executor_queue(registry, one_worker_pool):
    from gateway.platforms import api_server_runs as runs
    task = asyncio.ensure_future(runs.on_turn_executor(lambda: "x"))
    await asyncio.sleep(0)  # let the coroutine reach its submission
    assert registry.snapshot().active == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.to_thread(wait_until, lambda: registry.snapshot().active == 0, what="queued fire cancelled -> released")


async def _authority(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    monkeypatch.setattr(run, "_load_gateway_config", lambda: {"platform_toolsets": {"acp": []}})
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False,
                             _cached_agent_for=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    return await initialize_session_authority(runner, profile_id="fixture", instance_id="owner")


@pytest.mark.asyncio
async def test_authority_dispatch_reserves_for_work_verbs_and_not_for_passive(registry, tmp_path, monkeypatch):
    from gateway.session_controls import AuthorityConnection, RuntimeStoreError
    authority = await _authority(tmp_path, monkeypatch)
    try:
        connection = AuthorityConnection(authority, object(), {"user_id": "human", "profile_id": "fixture"})
        seen = {}
        real_handlers = AuthorityConnection.handlers

        def handlers(self):
            table = real_handlers(self)

            async def ping(ref, params):
                seen["ping"] = registry.snapshot().active
                return {"pong": True}

            async def info(ref, params):  # ``session.info`` is not PASSIVE => work
                seen["session.info"] = registry.snapshot().active
                raise RuntimeStoreError("permission_denied")
            table["ping"], table["session.info"] = ping, info
            return table
        monkeypatch.setattr(AuthorityConnection, "handlers", handlers)

        assert (await connection.dispatch({"id": 1, "method": "ping", "params": {}}))["result"] == {"pong": True}
        await connection.dispatch({"id": 2, "method": "session.info", "params": {"session_id": "s"}})
        assert seen["ping"] == 0  # passive verb: un-reserved
        assert seen["session.info"] == 1  # work verb: reserved before the handler body
        assert registry.snapshot().active == 0  # released when the handler finished
    finally:
        authority.db.close()
