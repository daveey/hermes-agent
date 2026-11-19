"""Physical lifetime contracts using real threads and executor queues."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextvars
import threading

import pytest

from hermes_cli.local_work import ACTIVE_SCOPE, Registry
from hermes_cli.local_work_threads import (
    ScopeAwareExecutor, TrackedThread, submit_tracked,
)


@pytest.fixture(autouse=True, params=[False, True])
def executor_accounting(request):
    from hermes_cli import loop_accounting

    if request.param:
        loop_accounting.instrument_executors()
    yield
    if request.param:
        loop_accounting.uninstrument()
        loop_accounting.reset()


def setup_owner(tmp_path):
    home = str(tmp_path)
    return Registry(homes=[home]), home


def test_thread_captures_constructor_scope_but_reserves_at_start(tmp_path):
    registry, home = setup_owner(tmp_path)
    entered, release = threading.Event(), threading.Event()
    observed = []
    marker = contextvars.ContextVar("test_marker")
    def target():
        observed.append((ACTIVE_SCOPE.get().home, marker.get()))
        entered.set()
        assert release.wait(5)
    with registry.scope("turn", home=home, source="parent"):
        marker.set("constructor")
        thread = TrackedThread(target=target)
        assert registry.snapshot().active == 1
    assert registry.snapshot().active == 0
    marker.set("starter")
    try:
        thread.start()
        assert entered.wait(5)
        assert registry.snapshot().active == 1
        with pytest.raises(RuntimeError):
            thread.start()
        assert registry.snapshot().active == 1
        thread.join(0)
        assert registry.snapshot().active == 1
    finally:
        release.set()
        thread.join(5)
    assert observed == [(home, "constructor")]
    assert registry.snapshot().active == 0
    assert registry.snapshot().unknowns == ()


def test_thread_start_failure_releases_once(tmp_path, monkeypatch):
    registry, home = setup_owner(tmp_path)
    thread = TrackedThread(target=lambda: None, registry=registry,
                           home=home, source="explicit")
    def fail(*args, **kwargs):
        raise RuntimeError("injected start failure")
    with monkeypatch.context() as patch:
        patch.setattr(threading, "_start_joinable_thread", fail)
        with pytest.raises(RuntimeError, match="injected"):
            thread.start()
    assert registry.snapshot().active == 0
    assert registry.snapshot().unknowns == ()
    thread.start()  # Stdlib permits retry when native thread creation failed.
    thread.join(5)
    assert not thread.is_alive()
    assert registry.snapshot().active == 0
    assert registry.snapshot().unknowns == ()


def test_interrupted_start_retains_pending_physical_thread(tmp_path, monkeypatch):
    registry, home = setup_owner(tmp_path)
    release, ran = threading.Event(), threading.Event()
    thread = TrackedThread(target=ran.set, registry=registry,
                           home=home, source="interrupted-start")
    bootstrap = thread._bootstrap
    def delayed_bootstrap():
        assert release.wait(5)
        bootstrap()
    def interrupt_wait(*args, **kwargs):
        raise KeyboardInterrupt("start waiter interrupted")
    monkeypatch.setattr(thread, "_bootstrap", delayed_bootstrap)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(thread._started, "wait", interrupt_wait)
            with pytest.raises(KeyboardInterrupt):
                thread.start()
        assert registry.snapshot().active == 1
        assert registry.snapshot().unknowns
    finally:
        release.set()
        assert thread._started.wait(5)
        thread.join(5)
    assert ran.is_set()
    assert registry.snapshot().active == 0


def test_scope_executor_preserves_target_keyword_arguments(tmp_path):
    registry, home = setup_owner(tmp_path)
    payload = {"home": "payload-home", "source": "payload-source",
               "registry": "payload-registry", "kind": "payload-kind"}
    with ScopeAwareExecutor(max_workers=1) as pool:
        with registry.scope("turn", home=home, source="parent"):
            assert pool.submit(lambda **kw: kw, **payload).result(5) == payload
    assert registry.snapshot().active == 0
    assert registry.snapshot().unknowns == ()


def test_executor_passive_epoch_and_scope_transfer(tmp_path):
    registry, home = setup_owner(tmp_path)
    with ScopeAwareExecutor(max_workers=1) as pool:
        epoch = registry.snapshot().epoch
        assert pool.submit(lambda: ACTIVE_SCOPE.get()).result(5) is None
        assert registry.snapshot().epoch == epoch
        with registry.scope("turn", home=home, source="parent"):
            assert pool.submit(lambda: ACTIVE_SCOPE.get().home).result(5) == home
    assert registry.snapshot().active == 0
    assert registry.snapshot().unknowns == ()


def test_queued_cancel_and_shutdown_release_once(tmp_path):
    registry, home = setup_owner(tmp_path)
    entered, release = threading.Event(), threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    def blocked():
        entered.set()
        assert release.wait(5)
    try:
        running = submit_tracked(pool, blocked, registry=registry,
                                 home=home, source="running")
        assert entered.wait(5)
        canceled = submit_tracked(pool, lambda: pytest.fail("canceled target ran"),
                                  registry=registry, home=home, source="queued")
        assert registry.snapshot().active == 2
        assert canceled.cancel()
        assert not running.cancel()
        assert registry.snapshot().active == 1
        queued = submit_tracked(pool, lambda: pytest.fail("shutdown target ran"),
                                registry=registry, home=home, source="shutdown")
        pool.shutdown(wait=False, cancel_futures=True)
        assert queued.cancelled()
        assert registry.snapshot().active == 1
    finally:
        release.set()
        pool.shutdown(wait=True)
    assert registry.snapshot().active == 0
    assert registry.snapshot().unknowns == ()


def test_failed_submit_keeps_enqueued_target_custody(tmp_path, monkeypatch):
    registry, home = setup_owner(tmp_path)
    entered, release, ran = (threading.Event() for _ in range(3))
    pool = ThreadPoolExecutor(max_workers=2)
    def old_worker():
        entered.set()
        assert release.wait(5)
    old = pool.submit(old_worker)
    assert entered.wait(5)
    def fail(*args, **kwargs):
        raise RuntimeError("additional worker start failed")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(threading, "_start_joinable_thread", fail)
            with pytest.raises(RuntimeError, match="additional worker"):
                submit_tracked(pool, ran.set, registry=registry,
                               home=home, source="queued-before-error")
        assert registry.snapshot().active == 1
        assert registry.snapshot().unknowns
        release.set()
        assert ran.wait(5)
        old.result(5)
    finally:
        release.set()
        pool.shutdown(wait=True)
    assert registry.snapshot().active == 0
    assert registry.snapshot().unknowns  # no optimistic recovery after uncertainty


def test_submit_to_shut_down_pool_releases_without_latching(tmp_path):
    """A stdlib pool refuses a submit after shutdown BEFORE enqueuing: release, no sticky unknown
    (RED on 95089d00fb: the reservation leaked and 'submit-outcome-uncertain' latched)."""
    registry, home = setup_owner(tmp_path)
    pool = ThreadPoolExecutor(max_workers=1)
    pool.shutdown(wait=True)
    with pytest.raises(RuntimeError, match="shutdown"):
        submit_tracked(pool, lambda: None, registry=registry, home=home, source="after-shutdown")
    snap = registry.snapshot()
    assert snap.active == 0 and snap.unknowns == ()


def test_audio_cancelled_queued_job_releases_and_never_runs(tmp_path, monkeypatch):
    """hermes_cli.web_routers.audio._run_reserved: cancelling the awaiting request while the job is
    still queued releases the reservation, and the job never runs (RED on 95089d00fb: token leaked)."""
    from hermes_cli import local_work
    from hermes_cli.web_routers import audio
    from tests.waiting import async_wait_until

    registry, home = setup_owner(tmp_path)
    monkeypatch.setattr(local_work, "_registry", registry)
    monkeypatch.setenv("HERMES_HOME", home)
    entered, release, ran = threading.Event(), threading.Event(), threading.Event()

    async def scenario():
        loop = asyncio.get_running_loop()
        pool = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(pool)
        blocker = loop.run_in_executor(None, lambda: (entered.set(), release.wait(5)))
        assert entered.wait(5)
        task = asyncio.ensure_future(audio._run_reserved("test.audio", ran.set))
        await async_wait_until(lambda: registry.snapshot().active == 1, timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert registry.snapshot().active == 0
        release.set()
        await blocker
        pool.shutdown(wait=True)

    asyncio.run(scenario())
    assert not ran.is_set()
    snap = registry.snapshot()
    assert snap.active == 0 and snap.unknowns == ()


def test_initializer_failure_releases_verified_never_started_target(tmp_path):
    registry, home = setup_owner(tmp_path)
    def fail_initializer():
        raise RuntimeError("initializer failed")
    with ThreadPoolExecutor(max_workers=1, initializer=fail_initializer) as pool:
        future = submit_tracked(pool, lambda: pytest.fail("target ran"),
                                registry=registry, home=home, source="broken-pool")
        with pytest.raises(RuntimeError):
            future.result(5)
    assert registry.snapshot().active == 0
    assert registry.snapshot().unknowns == ()


def test_canceled_async_waiter_does_not_release_physical_thread(tmp_path):
    registry, home = setup_owner(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def physical():
        entered.set()
        assert release.wait(5)
    async def scenario():
        loop = asyncio.get_running_loop()
        pool = ScopeAwareExecutor(max_workers=1)
        loop.set_default_executor(pool)
        try:
            with registry.scope("turn", home=home, source="async-owner"):
                task = asyncio.create_task(asyncio.to_thread(physical))
            # Yield once to schedule to_thread; the worker then signals directly.
            await asyncio.sleep(0)
            assert entered.wait(5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert registry.snapshot().active == 1
        finally:
            release.set()
            await loop.shutdown_default_executor()
    asyncio.run(scenario())
    assert registry.snapshot().active == 0
    assert registry.snapshot().unknowns == ()


def test_replaced_thread_class_is_honoured_and_latches_unknown(tmp_path, monkeypatch):
    """A replaced ``threading.Thread`` (test double, green threads) cannot carry custody: the
    spawn honours it and the registry latches unknown coverage instead of claiming tracking."""
    from agent.memory_provider import spawn_context_thread
    import hermes_cli.local_work_threads as lwt

    registry, home = setup_owner(tmp_path)

    class _Double:
        def __init__(self, target=None, **_options):
            self.target = target

        def start(self):
            return None

    monkeypatch.setattr(threading, "Thread", _Double)
    # Resolved through the main thread's MRO: an import under the double still finds stdlib's.
    assert lwt._real_thread_class() is lwt.TrackedThread.__mro__[1]
    assert lwt._real_thread_class() is not _Double
    with registry.scope("turn", home=home, source="parent"):
        thread = spawn_context_thread(lambda: None, name="t")
    assert isinstance(thread, _Double)
    assert "threading-thread-replaced" in registry.snapshot().unknowns
