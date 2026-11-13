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
