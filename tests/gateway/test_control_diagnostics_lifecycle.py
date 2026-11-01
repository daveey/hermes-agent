"""Control diagnostics must never take down the control listener or silence itself."""
import asyncio
import gc
import json
import logging
import threading
import types
import weakref

import pytest

from gateway.control_socket import GatewayControlServer


async def _exchange(server):
    reader, writer = await asyncio.open_unix_connection(str(server._bind_path))
    try:
        writer.write(json.dumps({"verb": "status"}).encode() + b"\n")
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 5))
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.platforms("posix")
def test_watchdog_thread_start_failure_keeps_listener_serving(tmp_path, monkeypatch, caplog):
    from gateway import control_diagnostics

    def broken_start(self):
        raise RuntimeError("can't start new thread")

    async def run():
        server = GatewayControlServer(tmp_path, verb_handlers={"status": lambda: {"alive": True}})
        original_init = control_diagnostics.LoopWatchdog.__init__
        made = []

        def init(self, loop):
            original_init(self, loop)
            self.thread.start = types.MethodType(broken_start, self.thread)
            made.append(self)

        monkeypatch.setattr(control_diagnostics.LoopWatchdog, "__init__", init)
        assert await server.start()
        try:
            assert server._loop_watchdog is None
            # The partially started watchdog scheduled a heartbeat; it must be cancelled.
            assert made and made[0]._stop.is_set()
            assert made[0]._heartbeat is None or made[0]._heartbeat.cancelled()
            assert (await _exchange(server))["result"]["alive"]
        finally:
            await server.stop()

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    assert "Control loop diagnostics unavailable" in caplog.text
    assert "can't start new thread" not in caplog.text


@pytest.mark.platforms("posix")
def test_watchdog_construction_failure_keeps_listener_serving(tmp_path, monkeypatch, caplog):
    from gateway import control_diagnostics

    def broken_init(self, loop):
        raise RuntimeError("no context thread")

    monkeypatch.setattr(control_diagnostics.LoopWatchdog, "__init__", broken_init)

    async def run():
        server = GatewayControlServer(tmp_path, verb_handlers={"status": lambda: {"alive": True}})
        assert await server.start()
        try:
            assert server._loop_watchdog is None
            assert (await _exchange(server))["result"]["alive"]
        finally:
            await server.stop()

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    assert "Control loop diagnostics unavailable" in caplog.text


def test_snapshot_failure_keeps_heartbeat_and_marks_snapshot_historical(monkeypatch, caplog):
    from gateway import control_diagnostics as diagnostics

    clock = [0.0]
    monkeypatch.setattr(diagnostics, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    original = asyncio.all_tasks
    fail = [False]

    def tasks(loop):
        if fail[0]:
            raise RuntimeError("PRIVATE-SNAPSHOT-DETAIL")
        return original(loop)

    monkeypatch.setattr(asyncio, "all_tasks", tasks)

    def check(watchdog):
        thread = threading.Thread(target=watchdog._check)
        thread.start()
        thread.join(3)
        assert not thread.is_alive()

    async def run():
        watchdog = diagnostics.LoopWatchdog(asyncio.get_running_loop())
        watchdog.repeat_interval = 0.0
        watchdog._beat()  # good snapshot at t=0
        try:
            fail[0] = True
            clock[0] = 5.0
            watchdog._heartbeat.cancel()
            watchdog._beat()  # loop is alive at t=5 but the snapshot fails
            assert watchdog._heartbeat is not None and not watchdog._heartbeat.cancelled()
            check(watchdog)  # due = 5.25, now = 5: a live loop must not warn
            assert "Control event loop blocked" not in caplog.text
            clock[0] = 7.0
            check(watchdog)  # genuinely late: warns with the historical snapshot labelled
        finally:
            watchdog.stop()

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    record, = [r for r in caplog.records if "Control event loop blocked" in r.getMessage()]
    assert record.task_snapshot_age_ms == pytest.approx(7000)
    assert record.task_snapshot_error == "RuntimeError"
    assert "run" in record.task_stacks
    assert "PRIVATE-SNAPSHOT-DETAIL" not in caplog.text


def test_first_snapshot_failure_reports_unavailable(monkeypatch, caplog):
    from gateway import control_diagnostics as diagnostics

    clock = [0.0]
    monkeypatch.setattr(diagnostics, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))

    def tasks(loop):
        raise RuntimeError("PRIVATE")

    monkeypatch.setattr(asyncio, "all_tasks", tasks)

    async def run():
        watchdog = diagnostics.LoopWatchdog(asyncio.get_running_loop())
        watchdog._beat()
        try:
            assert watchdog._heartbeat is not None
            clock[0] = 3.0
            thread = threading.Thread(target=watchdog._check)
            thread.start()
            thread.join(3)
        finally:
            watchdog.stop()

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    record, = [r for r in caplog.records if "Control event loop blocked" in r.getMessage()]
    assert record.task_snapshot_age_ms is None
    assert record.task_stacks == "unavailable"
    assert "PRIVATE" not in caplog.text


def test_pending_connection_task_is_strongly_referenced(tmp_path):
    from unittest.mock import Mock

    async def run():
        server = GatewayControlServer(tmp_path)
        task = server._connection_accepted(asyncio.StreamReader(), Mock())
        ref = weakref.ref(task)
        del task
        await asyncio.sleep(0)  # now suspended in readline() on a reader only it references
        gc.collect()
        task = ref()
        assert task is not None, "pending connection task was garbage collected"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert server._diagnostics.in_flight == 0

    asyncio.run(run())


@pytest.mark.platforms("posix")
def test_verb_metadata_recursion_error_still_replies(tmp_path, monkeypatch, caplog):
    # Python 3.12's json raises RecursionError on deeply nested input (3.14 raises a decode
    # error), so inject it into the metadata parser only; the real handler parses normally.
    from gateway import control_diagnostics

    def nested_too_deep(raw):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(control_diagnostics, "json", types.SimpleNamespace(loads=nested_too_deep))

    def status():
        threading.Event().wait(0.6)  # health: allow HX013 -- slow handler forces the latency record
        return {"alive": True}

    async def run():
        server = GatewayControlServer(tmp_path, verb_handlers={"status": status})
        assert await server.start()
        try:
            return await _exchange(server)
        finally:
            await server.stop()

    with caplog.at_level(logging.WARNING):
        reply = asyncio.run(run())
    assert reply["ok"] and reply["result"] == {"alive": True}
    records = [r for r in caplog.records if "Control request latency" in r.getMessage()]
    assert [r.verb for r in records] == ["unknown"]
