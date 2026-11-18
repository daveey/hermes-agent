"""MCP admission must wait for the owned loop, not merely Thread.start."""
import asyncio
import threading
from types import SimpleNamespace

from tests.waiting import wait_until


def test_first_call_waits_until_loop_can_accept_work(monkeypatch):
    from tools import mcp_tool as origin
    from tools import mcp_tool_loop as plumbing

    assert origin._mcp_loop is None
    entered, release, waiting, finished = (threading.Event() for _ in range(4))
    real_new = asyncio.new_event_loop
    loops, errors, results = [], [], []

    class ReadyEvent(threading.Event):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    def new_loop():
        loop = real_new()
        loops.append(loop)
        run = loop.run_forever

        def delayed_run():
            entered.set()
            assert release.wait(10), "test did not release owned loop"
            run()

        loop.run_forever = delayed_run
        return loop

    async def answer():
        return asyncio.get_running_loop()

    def caller():
        try:
            plumbing._ensure_mcp_loop()
            results.append(plumbing._run_on_mcp_loop(answer, timeout=5))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    monkeypatch.setattr(plumbing, "threading", SimpleNamespace(Lock=threading.Lock, Thread=threading.Thread, Event=ReadyEvent))
    monkeypatch.setattr(plumbing.asyncio, "new_event_loop", new_loop)
    worker = threading.Thread(target=caller)
    worker.start()
    try:
        assert entered.wait(5)
        # Either startup waits for actual readiness, or the old caller already failed.
        assert wait_until(lambda: waiting.is_set() or finished.is_set(), timeout=5)
        assert not finished.is_set(), f"admission returned before loop readiness: {errors}"
        release.set()
        worker.join(10)
        assert not worker.is_alive()
        assert not errors
        assert results == loops
        plumbing._ensure_mcp_loop()
        assert len(loops) == 1
    finally:
        release.set()
        worker.join(10)
        # Also reap the real loop on the RED path, after the delayed start resumes.
        for loop in loops:
            if not loop.is_closed():
                loop.call_soon_threadsafe(loop.stop)
        thread = origin._mcp_thread
        if thread is not None:
            thread.join(10)
            assert not thread.is_alive()
        for loop in loops:
            if not loop.is_closed():
                loop.close()
        origin._mcp_loop = origin._mcp_thread = None


def test_failed_thread_start_closes_unpublished_loop(monkeypatch):
    import pytest
    from tools import mcp_tool as origin
    from tools import mcp_tool_loop as plumbing

    loop = asyncio.new_event_loop()

    class FailedThread(threading.Thread):
        def start(self):
            raise RuntimeError("cannot start owned thread")

    monkeypatch.setattr(plumbing.asyncio, "new_event_loop", lambda: loop)
    monkeypatch.setattr(plumbing, "threading", SimpleNamespace(Lock=threading.Lock, Thread=FailedThread, Event=threading.Event))
    with pytest.raises(RuntimeError, match="cannot start owned thread"):
        plumbing._ensure_mcp_loop()
    assert loop.is_closed()
    assert origin._mcp_loop is origin._mcp_thread is None


def test_readiness_failure_reaps_late_start(monkeypatch):
    import pytest
    from tools import mcp_tool as origin
    from tools import mcp_tool_loop as plumbing

    entered, release = threading.Event(), threading.Event()
    loop = asyncio.new_event_loop()
    run = loop.run_forever
    threads = []

    def delayed_run():
        entered.set()
        assert release.wait(10)
        run()

    class NotReady(threading.Event):
        def wait(self, timeout=None):
            assert entered.wait(5)
            return False  # deterministically exhaust startup, without a wall-clock sleep

    class OwnedThread(threading.Thread):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            threads.append(self)

    loop.run_forever = delayed_run
    monkeypatch.setattr(plumbing.asyncio, "new_event_loop", lambda: loop)
    monkeypatch.setattr(plumbing, "threading", SimpleNamespace(Lock=threading.Lock, Thread=OwnedThread, Event=NotReady))
    try:
        with pytest.raises(RuntimeError, match="did not become ready"):
            plumbing._ensure_mcp_loop()
        assert origin._mcp_loop is origin._mcp_thread is None
    finally:
        release.set()
        for thread in threads:
            thread.join(10)
            assert not thread.is_alive()
    assert loop.is_closed()


def test_ready_callback_does_not_publish_an_already_stopped_loop(monkeypatch):
    import pytest
    from tools import mcp_tool as origin
    from tools import mcp_tool_loop as plumbing

    loop = asyncio.new_event_loop()
    threads = []
    loop.call_soon(loop.stop)

    class StoppedReady(threading.Event):
        def wait(self, timeout=None):
            result = super().wait(timeout)
            threads[0].join(5)
            assert not threads[0].is_alive()
            return result

    class OwnedThread(threading.Thread):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            threads.append(self)

    monkeypatch.setattr(plumbing.asyncio, "new_event_loop", lambda: loop)
    monkeypatch.setattr(plumbing, "threading", SimpleNamespace(Lock=threading.Lock, Thread=OwnedThread, Event=StoppedReady))
    try:
        with pytest.raises(RuntimeError, match="did not become ready"):
            plumbing._ensure_mcp_loop()
        assert origin._mcp_loop is origin._mcp_thread is None
        assert loop.is_closed()
    finally:
        for thread in threads:
            thread.join(5)
        loop.close()
        origin._mcp_loop = origin._mcp_thread = None
