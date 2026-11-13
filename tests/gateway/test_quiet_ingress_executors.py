"""Gateway ingress/executor reservations: real adapter, runner executor helpers and turn worker."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import local_work
from hermes_cli.local_work import Registry
from hermes_cli.local_work_threads import ScopeAwareExecutor, install_scope_default_executor, work_scope
from hermes_constants import hermes_home_key
from tests.waiting import wait_until


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reg = Registry(homes=[hermes_home_key(tmp_path)])
    monkeypatch.setattr(local_work, "_registry", reg)
    return reg


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="t"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def _event():
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", chat_type="dm", user_id="u1")
    return MessageEvent(text="hello", source=source, message_id="m1")


@pytest.mark.asyncio
async def test_adapter_ingress_reserves_before_preflight_and_until_task_ends(registry, monkeypatch):
    adapter = _Adapter()
    in_handler, release = asyncio.Event(), asyncio.Event()
    at_preflight, at_create = [], []

    async def handler(_event):
        in_handler.set()
        await release.wait()

    adapter.set_message_handler(handler)
    real_drop = adapter._drop_unresolved

    def drop(event):
        at_preflight.append(registry.snapshot().active)
        return real_drop(event)
    monkeypatch.setattr(adapter, "_drop_unresolved", drop)
    real_create = asyncio.create_task

    def create(coro, **kw):
        at_create.append(registry.snapshot().active)
        return real_create(coro, **kw)
    monkeypatch.setattr(asyncio, "create_task", create)

    assert registry.snapshot().active == 0
    await adapter.handle_message(_event())
    assert at_preflight and at_preflight[0] >= 1  # reserved at handle_message entry
    assert at_create and at_create[0] >= 2  # entry scope + task reservation before create_task
    await asyncio.wait_for(in_handler.wait(), 5)
    # handle_message returned; only the background processing task still holds ingress custody
    assert registry.snapshot().active >= 1 and registry.snapshot().per_kind.get("gateway_ingress", 0) >= 1
    release.set()
    while adapter._background_tasks:
        await asyncio.wait(list(adapter._background_tasks), timeout=5)
    wait_until(lambda: registry.snapshot().active == 0, what="ingress release")
    assert "unmatched-finish" not in registry.snapshot().unknowns


@pytest.mark.asyncio
async def test_canceled_waiter_leaves_pool_thread_counted_housekeeping_stays_passive(registry):
    gate_pool, gate_hk = threading.Event(), threading.Event()
    started_pool, started_hk = threading.Event(), threading.Event()
    runner = GatewayRunner.__new__(GatewayRunner)
    pool = ScopeAwareExecutor(max_workers=2)
    hk = ScopeAwareExecutor(max_workers=2)
    runner._get_executor = lambda: pool
    runner._get_housekeeping_executor = lambda: hk

    def blocking(started, gate):
        started.set()
        assert gate.wait(10)

    work = asyncio.ensure_future(GatewayRunner._run_in_executor_with_context(runner, blocking, started_pool, gate_pool))
    maintenance = asyncio.ensure_future(
        GatewayRunner._run_housekeeping_in_executor(runner, blocking, started_hk, gate_hk))
    await asyncio.to_thread(started_pool.wait, 5)
    await asyncio.to_thread(started_hk.wait, 5)
    assert registry.snapshot().per_kind == {"gateway_pool": 1}  # housekeeping thread is live but uncounted
    work.cancel()
    maintenance.cancel()
    await asyncio.gather(work, maintenance, return_exceptions=True)
    assert registry.snapshot().active == 1  # waiter canceled; the physical thread continues
    gate_pool.set()
    gate_hk.set()
    wait_until(lambda: registry.snapshot().active == 0, what="thread exit release")
    pool.shutdown(wait=True)
    hk.shutdown(wait=True)


@pytest.mark.asyncio
async def test_default_executor_to_thread_counts_only_inside_a_scope(registry, tmp_path):
    loop = asyncio.get_running_loop()
    assert install_scope_default_executor(loop) in (True, False)
    gate_scoped, gate_passive = threading.Event(), threading.Event()
    started_scoped, started_passive = threading.Event(), threading.Event()

    def blocking(started, gate):
        started.set()
        assert gate.wait(10)

    home = hermes_home_key(tmp_path)

    async def scoped():
        with work_scope("gateway_ingress", "test:scoped", home):
            await asyncio.to_thread(blocking, started_scoped, gate_scoped)

    passive = asyncio.ensure_future(asyncio.to_thread(blocking, started_passive, gate_passive))
    await asyncio.to_thread(started_passive.wait, 5)
    assert registry.snapshot().active == 0  # maintenance to_thread: passive
    task = asyncio.ensure_future(scoped())
    await asyncio.to_thread(started_scoped.wait, 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # the scope exited with the canceled task, but the physical thread kept its own token
    assert registry.snapshot().per_kind == {"executor": 1}
    gate_scoped.set()
    gate_passive.set()
    await passive
    wait_until(lambda: registry.snapshot().active == 0, what="scoped thread exit")


@pytest.mark.asyncio
async def test_abandoned_timed_out_turn_worker_stays_counted_until_thread_exits(registry):
    from gateway.session_runtime_workers import start_turn_worker

    pool = ScopeAwareExecutor(max_workers=1)
    runner = SimpleNamespace(_get_executor=lambda: pool)
    worker = SimpleNamespace(worker_done=threading.Event(), executor_task=None)
    entered, release = threading.Event(), threading.Event()

    def run_sync():
        try:
            entered.set()
            assert release.wait(10)
        finally:
            worker.worker_done.set()

    before = registry.snapshot()
    start_turn_worker(runner, worker, [None], run_sync)
    assert registry.snapshot().active == 1  # reserved synchronously, before the task ever steps
    await asyncio.to_thread(entered.wait, 5)
    with pytest.raises(asyncio.TimeoutError):  # inactivity timeout abandons the waiter
        await asyncio.wait_for(asyncio.shield(worker.executor_task), 0.05)
    worker.executor_task.cancel()
    await asyncio.gather(worker.executor_task, return_exceptions=True)
    assert registry.snapshot().per_kind == {"gateway_worker": 1}  # asyncio abandon is not release
    release.set()
    wait_until(lambda: registry.snapshot().active == 0, what="turn thread exit")
    assert registry.snapshot().epoch > before.epoch
    pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_sub_second_turn_worker_bumps_epoch(registry):
    from gateway.session_runtime_workers import start_turn_worker

    pool = ScopeAwareExecutor(max_workers=1)
    runner = SimpleNamespace(_get_executor=lambda: pool)
    worker = SimpleNamespace(worker_done=threading.Event(), executor_task=None)
    before = registry.snapshot()
    start_turn_worker(runner, worker, [None], worker.worker_done.set)
    await asyncio.wait_for(worker.executor_task, 5)
    wait_until(lambda: registry.snapshot().active == 0, what="release")
    assert registry.snapshot().epoch >= before.epoch + 2
    pool.shutdown(wait=True)
