"""Direct pool submissions retain logical CPU names through context and custody."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from functools import partial
import time

import pytest

from hermes_cli import loop_accounting as la
from hermes_cli import local_work_threads as threads
from hermes_cli.local_work import ACTIVE_SCOPE, Registry
from tools.daemon_pool import DaemonThreadPoolExecutor


@pytest.fixture(autouse=True)
def accounting():
    la.stop()
    la.reset()
    yield
    la.uninstrument()
    la.stop()
    la.reset()


def burn(seconds):
    end = time.thread_time() + seconds
    while time.thread_time() < end:
        pass


@pytest.mark.parametrize("pool_type", [ThreadPoolExecutor, threads.ScopeAwareExecutor, DaemonThreadPoolExecutor])
@pytest.mark.parametrize("tracked", [False, True])
def test_direct_submit_names_function_and_preserves_context(tmp_path, pool_type, tracked):
    la.instrument_executors()
    registry = Registry(homes=[str(tmp_path)])
    value = ContextVar("value", default="unset")
    value.set("submitted")

    def job(positional, *, keyword):
        burn(0.02)
        with la.pass_("pool.inner"):
            burn(0.02)
        return positional, keyword, value.get()

    call = partial(copy_context().run, job, 7, keyword=9)
    with pool_type(max_workers=1) as pool:
        if tracked:
            future = threads.submit_tracked(pool, call, registry=registry, home=str(tmp_path), source="test")
        else:
            future = pool.submit(call)
        assert future.result(timeout=10) == (7, 9, "submitted")
    names = {k: v for k, v in la.counters().items() if k.startswith("thread/")}
    assert len(names) == 1, names
    name, stat = next(iter(names.items()))
    assert name.endswith(".<locals>.job"), names
    assert stat["runs"] == 1
    assert stat["cpu_s"] >= 0.015
    assert la.counters()["pool.inner"]["cpu_s"] >= 0.015
    assert la.snapshot()["process"]["accounted_cpu_s"] == pytest.approx(
        stat["cpu_s"] + la.counters()["pool.inner"]["cpu_s"], abs=0.005)
    assert registry.snapshot().active == 0


@pytest.mark.parametrize("route", ["asyncio", "custody"])
def test_layered_executor_hooks_count_one_run(monkeypatch, tmp_path, route):
    la.instrument_executors()
    registry = Registry(homes=[str(tmp_path)])
    monkeypatch.setattr(threads, "installed_registry", lambda: registry)
    monkeypatch.setattr(threads, "current_home", lambda: str(tmp_path))

    def job():
        return 42

    async def run():
        if route == "asyncio":
            return await asyncio.to_thread(job)
        return await threads.run_in_pool(None, copy_context(), job)

    assert asyncio.run(run()) == 42
    names = {k: v for k, v in la.counters().items() if k.startswith("thread/")}
    assert len(names) == 1, names
    assert next(iter(names)).endswith(".<locals>.job")
    assert next(iter(names.values()))["runs"] == 1
    assert registry.snapshot().active == 0


@pytest.mark.parametrize("pool_type", [ThreadPoolExecutor, threads.ScopeAwareExecutor, DaemonThreadPoolExecutor])
def test_tracked_failure_keeps_custody_and_original_exception(tmp_path, pool_type):
    la.instrument_executors()
    registry = Registry(homes=[str(tmp_path)])
    failure = ValueError("original failure")

    def failing_job():
        assert registry.snapshot().active > 0
        owner = ACTIVE_SCOPE.get()
        assert owner is not None and owner.home == str(tmp_path)
        raise failure

    with pool_type(max_workers=1) as pool:
        future = threads.submit_tracked(pool, failing_job, registry=registry,
                                        home=str(tmp_path), source="test")
        with pytest.raises(ValueError) as exc:
            future.result(timeout=10)
        assert exc.value is failure
    assert registry.snapshot().active == 0
    names = {k: v for k, v in la.counters().items() if k.startswith("thread/")}
    assert len(names) == 1
    assert next(iter(names)).endswith(".failing_job")
    assert next(iter(names.values()))["runs"] == 1


def test_automatic_custody_and_prescoped_context_count_each_submission(tmp_path):
    la.instrument_executors()
    registry = Registry(homes=[str(tmp_path)])

    def job(value):
        owner = ACTIVE_SCOPE.get()
        assert owner is not None and owner.home == str(tmp_path)
        assert registry.snapshot().active == 2  # submitting scope + physical work
        return value

    with threads.ScopeAwareExecutor(max_workers=1) as pool:
        with registry.scope("turn", home=str(tmp_path), source="test"):
            for value in (1, 2):
                call = partial(copy_context().run, la.scoped_call("thread", job), value)
                assert pool.submit(call).result(timeout=10) == value
    assert registry.snapshot().active == 0
    names = {k: v for k, v in la.counters().items() if k.startswith("thread/")}
    assert len(names) == 1
    assert next(iter(names)).endswith(".job")
    assert next(iter(names.values()))["runs"] == 2


def test_daemon_pool_context_is_captured_for_each_submission():
    la.instrument_executors()
    value = ContextVar("daemon-value", default="unset")
    with DaemonThreadPoolExecutor(max_workers=1) as pool:
        for expected in ("first", "second"):
            value.set(expected)
            assert pool.submit(value.get).result(timeout=10) == expected


def test_submit_hook_restores_original_and_honors_disabled_scopes(monkeypatch):
    original = ThreadPoolExecutor.submit
    assert la.instrument_executors()
    assert ThreadPoolExecutor.submit is not original
    assert not la.instrument_executors()
    monkeypatch.setitem(la._state, "scopes", False)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(lambda: 42).result(timeout=10) == 42
    assert not {k for k in la.counters() if k.startswith("thread/")}
    la.uninstrument()
    assert ThreadPoolExecutor.submit is original
    la.uninstrument()
