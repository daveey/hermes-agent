"""Control latency and live blocked-loop diagnostics contracts."""
import asyncio
import json
import logging
import threading

import pytest

from gateway.control_socket import GatewayControlServer


async def exchange(server, verb="status", params=None):
    reader, writer = await asyncio.open_unix_connection(str(server._bind_path))
    try:
        writer.write(json.dumps({"verb": verb, "params": params}).encode() + b"\n")
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 5))
    finally:
        writer.close()
        await writer.wait_closed()


def latency_records(caplog):
    return [r for r in caplog.records if "Control request latency" in r.getMessage()]


@pytest.mark.platforms("posix")
def test_slow_real_verb_warns_without_parameters(tmp_path, caplog):
    def status(params):
        threading.Event().wait(0.6)  # health: allow HX013 -- real handler latency is the behavior under test
        return {"done": True}

    async def run():
        server = GatewayControlServer(tmp_path, verb_handlers={"status": status})
        from gateway.control_dispatch import ControlRequestDispatcher
        server._request_dispatch = ControlRequestDispatcher()
        assert await server.start()
        try:
            assert (await exchange(server, params={"secret": "NEVER-LOG-ME"}))["ok"]
        finally:
            await server.stop()

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    records = latency_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.verb == "status"
    assert record.run_ms >= 500
    assert record.total_ms >= record.run_ms
    assert record.queue_wait_ms >= 0
    assert record.in_flight >= 1
    assert "NEVER-LOG-ME" not in caplog.text


@pytest.mark.platforms("posix")
def test_watchdog_captures_while_loop_blocked_and_stops(tmp_path, caplog):
    observed = threading.Event()

    class Observer(logging.Handler):
        def emit(self, record):
            if "Control event loop blocked" in record.getMessage():
                observed.set()

    observer = Observer()
    log = logging.getLogger("gateway.control_diagnostics")
    log.addHandler(observer)

    async def run():
        server = GatewayControlServer(tmp_path)
        assert await server.start()
        try:
            # Deliberately blocks THIS loop; only the independent thread can release it.
            assert observed.wait(4), "no warning captured while the loop was blocked"
            watchdog = server._loop_watchdog
            assert watchdog is not None
            assert watchdog.thread.daemon
        finally:
            await server.stop()
        assert not watchdog.thread.is_alive()
        await server.stop()

    try:
        with caplog.at_level(logging.WARNING):
            asyncio.run(run())
    finally:
        log.removeHandler(observer)
    records = [r for r in caplog.records if "Control event loop blocked" in r.getMessage()]
    assert len(records) == 1
    assert "test_watchdog_captures_while_loop_blocked_and_stops" in records[0].thread_stacks
    assert "run" in records[0].task_stacks
    assert records[0].delay_ms > 1000


@pytest.mark.platforms("posix")
def test_saturated_executor_queue_and_fast_quiet(tmp_path, caplog):
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import patch
    from tests.waiting import async_wait_until

    release = threading.Event()
    entered = [threading.Event() for _ in range(4)]

    def occupy(event):
        event.set()
        release.wait(5)

    async def run(pool):
        server = GatewayControlServer(tmp_path, verb_handlers={"status": lambda: {"ok": True}})
        assert await server.start()
        try:
            assert (await exchange(server))["ok"]
            assert not latency_records(caplog)
            jobs = [pool.submit(occupy, event) for event in entered]
            assert all(event.wait(3) for event in entered)
            pending = asyncio.create_task(exchange(server))
            await async_wait_until(lambda: server._diagnostics.in_flight == 1, timeout=3)
            timer = threading.Timer(0.7, release.set)
            timer.start()
            try:
                assert (await pending)["ok"]
            finally:
                release.set()
                timer.join()
            for job in jobs:
                job.result(3)
            assert server._diagnostics.in_flight == 0
        finally:
            release.set()
            await server.stop()

    with caplog.at_level(logging.WARNING), ThreadPoolExecutor(max_workers=4) as pool:
        with patch("gateway.control_dispatch.ThreadPoolExecutor", return_value=pool):
            asyncio.run(run(pool))
    records = latency_records(caplog)
    assert len(records) == 1
    assert records[0].queue_wait_ms >= 500
    assert records[0].total_ms >= records[0].queue_wait_ms


def test_callback_cancellation_before_start_closes_and_accounts(tmp_path):
    from unittest.mock import Mock

    async def run():
        server = GatewayControlServer(tmp_path)
        writer = Mock()
        task = server._connection_accepted(asyncio.StreamReader(), writer)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert server._diagnostics.in_flight == 0
        writer.close.assert_called_once()

    asyncio.run(run())


def test_timing_cancelled_running_work_is_counted_until_completion(caplog):
    from concurrent.futures import ThreadPoolExecutor
    from gateway.control_diagnostics import ControlDiagnostics

    entered, release = threading.Event(), threading.Event()
    diagnostics = ControlDiagnostics()
    timing = diagnostics.accepted()

    def handler():
        entered.set()
        release.wait(3)
        raise ValueError("private-error")

    with ThreadPoolExecutor(max_workers=1) as pool:
        timing.enqueue()
        future = pool.submit(timing.run, handler)
        future.add_done_callback(timing.work_done)
        try:
            assert entered.wait(3)
            timing.finish()
            timing.finish()
            assert diagnostics.in_flight == 1
        finally:
            release.set()
        with pytest.raises(ValueError):
            future.result(3)
    assert diagnostics.in_flight == 0
    assert "private-error" not in caplog.text


@pytest.mark.parametrize("raw", [b'{"verb":"secret\\\\nattack"}', b'{"verb":[]}', b'[]', b'bad', b'\xff'])
def test_unknown_verb_redacted(raw):
    from gateway.control_diagnostics import safe_verb
    assert safe_verb(raw, {"status"}) == "unknown"
    assert safe_verb(b'{"verb":"status","params":{"secret":1}}', {"status"}) == "status"


def test_watchdog_rate_limit_and_loop_owned_snapshot(monkeypatch, caplog):
    import types
    from gateway import control_diagnostics as diagnostics

    clock = [0.0]
    monkeypatch.setattr(diagnostics, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    original = asyncio.all_tasks
    owner = threading.get_ident()

    def tasks(loop):
        assert threading.get_ident() == owner
        return original(loop)

    monkeypatch.setattr(asyncio, "all_tasks", tasks)

    async def run():
        watchdog = diagnostics.LoopWatchdog(asyncio.get_running_loop())
        watchdog._beat()
        try:
            for now in (2, 3, 10, 32):
                clock[0] = now
                thread = threading.Thread(target=watchdog._check)
                thread.start()
                thread.join(3)
                assert not thread.is_alive()
        finally:
            watchdog.stop()

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    assert len([r for r in caplog.records if "Control event loop blocked" in r.getMessage()]) == 2


@pytest.mark.platforms("posix")
def test_failed_start_has_no_watchdog(tmp_path):
    async def run():
        server = GatewayControlServer(tmp_path / "missing")
        assert not await server.start()
        assert server._loop_watchdog is None
        await server.stop()
    asyncio.run(run())


@pytest.mark.parametrize("verb", ["status", "secret-unknown"])
def test_callback_start_delay_included_and_error_private(tmp_path, caplog, verb):
    from unittest.mock import AsyncMock, Mock

    def status(params):
        raise ValueError(params["secret"])

    async def run():
        server = GatewayControlServer(tmp_path, verb_handlers={"status": status})
        from gateway.control_dispatch import ControlRequestDispatcher
        server._request_dispatch = ControlRequestDispatcher()
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps({"verb": verb, "params": {"secret": "PRIVATE"}}).encode() + b"\n")
        writer = Mock()
        writer.drain = AsyncMock()
        server._posix_peer_subject = lambda writer: None
        task = server._connection_accepted(reader, writer)
        threading.Event().wait(0.6)  # health: allow HX013 -- callback-to-coroutine startup delay is under test
        await task
        assert not json.loads(writer.write.call_args.args[0])["ok"]
        assert server._diagnostics.in_flight == 0
        server._request_dispatch.close()

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    record, = latency_records(caplog)
    assert record.total_ms >= 500
    assert record.verb == ("status" if verb == "status" else "unknown")
    assert "PRIVATE" not in caplog.text
    assert "secret-unknown" not in caplog.text


def test_cancelled_queued_work_accounts_once():
    from concurrent.futures import Future
    from gateway.control_diagnostics import ControlDiagnostics

    diagnostics = ControlDiagnostics()
    timing = diagnostics.accepted()
    timing.enqueue()
    future = Future()
    future.add_done_callback(timing.work_done)
    assert future.cancel()
    assert diagnostics.in_flight == 1
    timing.finish()
    timing.finish()
    assert diagnostics.in_flight == 0


def test_cancelled_connection_still_reports_slow_running_handler(tmp_path, caplog):
    from unittest.mock import AsyncMock, Mock
    from tests.waiting import async_wait_until

    entered, release = threading.Event(), threading.Event()

    def status():
        entered.set()
        release.wait(5)
        return {}

    async def run():
        server = GatewayControlServer(tmp_path, verb_handlers={"status": status})
        from gateway.control_dispatch import ControlRequestDispatcher
        server._request_dispatch = ControlRequestDispatcher()
        server._posix_peer_subject = lambda writer: None
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"verb":"status"}\n')
        writer = Mock(drain=AsyncMock())
        task = server._connection_accepted(reader, writer)
        await async_wait_until(entered.is_set, timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert server._diagnostics.in_flight == 1
        timer = threading.Timer(0.6, release.set)
        timer.start()
        try:
            await async_wait_until(lambda: server._diagnostics.in_flight == 0, timeout=3)
        finally:
            release.set()
            timer.join()
            server._request_dispatch.close()

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    record, = latency_records(caplog)
    assert record.run_ms >= 500
    assert record.total_ms >= record.run_ms

