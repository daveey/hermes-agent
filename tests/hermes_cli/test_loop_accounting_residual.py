"""Loop accounting residual: per-thread-family unaccounted CPU and named call-site scopes (t_01a1dee5bc2c9d6a)."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types

import pytest

from hermes_cli import loop_accounting as la

linux_procfs = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="per-thread CPU needs procfs here")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    la.stop()
    la.reset()
    yield
    la.uninstrument()
    la.stop()
    la.reset()


def _burn(seconds: float) -> None:
    end = time.thread_time() + seconds
    while time.thread_time() < end:
        pass


def _hold(name: str, work) -> tuple[threading.Thread, threading.Event]:
    """A live thread ``name`` that runs ``work`` then stays alive until the returned event is set."""
    done, release = threading.Event(), threading.Event()

    def body() -> None:
        work()
        done.set()
        release.wait(30)

    th = threading.Thread(target=body, name=name, daemon=True)
    th.start()
    assert done.wait(30)
    return th, release


def test_family_of_strips_suffixes():
    assert la.family_of("asyncio_3") == "asyncio"
    assert la.family_of("Thread-7 (run)") == "Thread"
    assert la.family_of("AnyIO worker thread") == "AnyIO worker thread"
    assert la.family_of("hermes-gateway-default") == "hermes-gateway-default"
    assert la.family_of("MainThread") == "MainThread"
    assert la.family_of("worker_0x7f3a9c") == "worker"


@linux_procfs
def test_unscoped_thread_cpu_is_that_familys_unaccounted_and_scoped_is_not():
    def unscoped():
        _burn(0.4)

    def scoped():
        with la.pass_("scoped.pass"):
            _burn(0.4)

    _t1, r1 = _hold("unscoped-fam_1", unscoped)
    _t2, r2 = _hold("scoped-fam_1", scoped)
    try:
        fams = la.snapshot()["residual"]["families"]
    finally:
        r1.set()
        r2.set()
    assert abs(fams["unscoped-fam"]["unaccounted_cpu_s"] - 0.4) <= 0.04
    assert fams["scoped-fam"]["unaccounted_cpu_s"] <= 0.04
    assert fams["scoped-fam"]["accounted_cpu_s"] >= 0.35


@linux_procfs
def test_family_unaccounted_sums_to_process_residual():
    la.reset()
    def work():
        _burn(0.3)
    _t1, r1 = _hold("sumfam_1", work)
    _t2, r2 = _hold("sumfam_2", lambda: la.record("rec", cpu_s=0.0))
    try:
        with la.pass_("main.scoped"):
            _burn(0.2)
        snap = la.snapshot()
    finally:
        r1.set()
        r2.set()
    res, proc = snap["residual"], snap["process"]
    want = proc["cpu_s"] - proc["accounted_cpu_s"]
    assert want > 0.25
    assert res["unaccounted_cpu_s"] == pytest.approx(want, rel=0.05)
    assert res["process_unaccounted_cpu_s"] == pytest.approx(want, abs=0.001)
    assert res["families"]["sumfam"]["threads"] == 2


@linux_procfs
def test_exited_thread_lands_in_exited_bucket_never_negative():
    def short():
        _burn(0.3)

    th = threading.Thread(target=short, name="gone_1")
    th.start()
    th.join()
    snap = la.snapshot()
    res = snap["residual"]
    assert "gone" not in res["families"]
    assert res["exited"]["cpu_s"] >= 0.25
    assert res["exited"]["unaccounted_cpu_s"] >= 0.25
    assert all(f["unaccounted_cpu_s"] >= 0 for f in res["families"].values())


def test_nested_scopes_are_not_counted_twice():
    with la.scope("outer"):
        _burn(0.05)
        with la.scope("inner"):
            _burn(0.05)
            with la.pass_("pass.inside"):
                _burn(0.05)
    c = la.counters()
    assert c["inner"]["cpu_s"] >= 0.04 and c["pass.inside"]["cpu_s"] >= 0.04
    # a scope books only what its nested pass/scope did not
    assert c["inner"]["cpu_s"] < 0.09 and c["outer"]["cpu_s"] < 0.09
    total = sum(c[k]["cpu_s"] for k in ("outer", "inner", "pass.inside"))
    assert la.snapshot()["process"]["accounted_cpu_s"] == pytest.approx(total, rel=0.05)
    assert 0.13 <= total <= 0.2


def test_executor_scopes_name_to_thread_and_anyio_functions():
    la.instrument_executors()

    def asyncio_job():
        _burn(0.02)

    def anyio_job():
        _burn(0.02)

    async def main():
        await asyncio.to_thread(asyncio_job)
        import anyio.to_thread
        await anyio.to_thread.run_sync(anyio_job)

    asyncio.run(main())
    c = la.counters()
    names = {k: v for k, v in c.items() if k.startswith("thread/")}
    assert any(k.endswith("asyncio_job") and v["runs"] == 1 and v["cpu_s"] >= 0.015 for k, v in names.items()), names
    assert any(k.endswith("anyio_job") and v["runs"] == 1 and v["cpu_s"] >= 0.015 for k, v in names.items()), names


def test_executor_scope_does_not_double_count_an_accounted_route():
    la.instrument_executors()

    def route():
        with la.pass_("route/x"):
            _burn(0.03)

    async def main():
        await asyncio.to_thread(route)

    asyncio.run(main())
    c = la.counters()
    scope_key = next(k for k in c if k.startswith("thread/") and k.endswith("route"))
    assert c["route/x"]["cpu_s"] >= 0.025
    assert c[scope_key]["runs"] == 1 and c[scope_key]["cpu_s"] < 0.02
    assert la.snapshot()["process"]["accounted_cpu_s"] < 0.06


def test_protected_aux_provider_worker_scope_keyed_by_operation(monkeypatch):
    from agent import auxiliary_client as ac

    token = ac._RELAY_AUX_CALL_CONTEXT.set({"task": "compression"})
    try:
        with ac.aux_interrupt_protection(cancel_check=lambda: False):
            out = ac._run_protected_sync_provider_call(lambda kw: (_burn(0.03), "ok")[1], {})
    finally:
        ac._RELAY_AUX_CALL_CONTEXT.reset(token)
    assert out == "ok"
    st = la.counters()["aux_provider/compression"]
    assert st["runs"] == 1 and st["cpu_s"] >= 0.025


def _fake_plugin(monkeypatch, name: str, path: str, **attrs):
    mod = types.ModuleType(name)
    mod.__file__ = path
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def test_plugin_sites_user_terminal_sweep_jobs_and_loop_watch_subcall(monkeypatch):
    calls = []

    def sweep_jobs(skip=None):
        _burn(0.02)
        calls.append(skip)

    class LoopWatch:
        def check_once(self):
            _burn(0.02)

    ut = _fake_plugin(monkeypatch, "fake_ut", "/x/plugins/user-terminal/__init__.py", sweep_jobs=sweep_jobs)
    lw = _fake_plugin(monkeypatch, "fake_lw", "/x/plugins/loop-watch/watch.py", LoopWatch=LoopWatch)
    assert la.instrument_plugins() == 2
    assert la.instrument_plugins() == 0  # idempotent
    ut.sweep_jobs(skip="s")
    lw.LoopWatch().check_once()
    assert calls == ["s"]
    c = la.counters()
    assert c["plugin/user-terminal.sweep_jobs"]["runs"] == 1 and c["plugin/user-terminal.sweep_jobs"]["cpu_s"] >= 0.015
    assert c["plugin/loop-watch.check_once"]["runs"] == 1 and c["plugin/loop-watch.check_once"]["cpu_s"] >= 0.015


def test_main_loop_callback_outside_any_scope_is_named():
    la.instrument_callbacks()

    async def burner_task():
        _burn(0.05)

    def plain_callback():
        _burn(0.03)

    async def scoped_task():
        with la.pass_("scoped.in.loop"):
            _burn(0.03)

    async def main():
        asyncio.get_running_loop().call_soon(plain_callback)
        await asyncio.create_task(burner_task())
        await asyncio.create_task(scoped_task())
        await asyncio.sleep(0)

    asyncio.run(main())
    c = la.counters()
    keys = {k: v for k, v in c.items() if k.startswith("loop_cb/")}
    burner = next(v for k, v in keys.items() if k.endswith("burner_task"))
    plain = next(v for k, v in keys.items() if k.endswith("plain_callback"))
    assert burner["cpu_s"] >= 0.045 and plain["cpu_s"] >= 0.025
    # CPU a pass already accounted is not booked again under the callback
    scoped = next(v for k, v in keys.items() if k.endswith("scoped_task"))
    assert scoped["cpu_s"] < 0.02 and c["scoped.in.loop"]["cpu_s"] >= 0.025


def test_callback_keys_are_capped_with_an_other_bucket():
    la.instrument_callbacks()

    async def main():
        loop = asyncio.get_running_loop()
        for i in range(la.MAX_CALLBACK_KEYS + 20):
            ns = {}
            exec(f"def cb_{i}(): pass", ns)
            loop.call_soon(ns[f"cb_{i}"])
        await asyncio.sleep(0.05)

    asyncio.run(main())
    keys = [k for k in la.counters() if k.startswith("loop_cb/")]
    assert la.CALLBACK_OTHER in keys
    assert len(keys) <= la.MAX_CALLBACK_KEYS + 1


def test_old_snapshot_keys_unchanged():
    snap = la.snapshot()
    for key in ("version", "role", "pid", "process", "threads", "loops"):
        assert key in snap
    for key in ("cpu_s", "accounted_cpu_s", "unaccounted_live_cpu_s", "exited_threads_cpu_s"):
        assert key in snap["process"]
