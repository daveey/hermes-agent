"""Bounded wall attribution, not an inference from a post-delay stack."""
import asyncio
import datetime
import functools
import json

import time

import pytest

from hermes_cli import loop_accounting as la


@pytest.fixture(autouse=True)
def fresh():
    la.stop()
    la.uninstrument()
    la.reset()
    yield
    la.uninstrument()
    la.reset()


def blocked(secret=None):
    # Controlled sleeping callback is the fault injection, not synchronization.
    time.sleep(0.51)  # health: allow HX013 -- deliberately reproduce blocked event-loop wall time


def test_sleep_records_wall_not_cpu_and_executor_wait_is_not_a_callback():
    la.instrument_callbacks()
    async def main():
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        def block_and_finish():
            blocked("private-request-value")
            done.set_result(None)
        loop.call_soon(block_and_finish)
        await done
        await loop.run_in_executor(None, blocked)
    asyncio.run(main())
    snap = la.snapshot()
    records = snap["slow_callbacks"]["recent"]
    own = [r for r in records if "block_and_finish" in r["identity"]]
    assert len(own) == 1
    assert own[0]["wall_s"] >= 0.5
    assert own[0]["inclusive_cpu_s"] < 0.2
    assert datetime.datetime.fromisoformat(own[0]["occurred_at"]).tzinfo is not None
    assert not any("main" == r["identity"].split(":")[-1] for r in records)
    assert "private-request-value" not in json.dumps(snap["slow_callbacks"])


def test_inclusive_cpu_does_not_double_book_nested_pass(monkeypatch):
    from hermes_cli import loop_callback_latency as latency
    monkeypatch.setattr(latency, "THRESHOLD_S", 0.0)
    loop = asyncio.new_event_loop()
    def work():
        with la.pass_("nested"):
            end = time.thread_time() + 0.02
            while time.thread_time() < end:
                pass
    la.instrument_callbacks()
    try:
        asyncio.Handle(work, (), loop)._run()
        record = latency.snapshot()["recent"][-1]
        assert record["inclusive_cpu_s"] >= 0.02
        assert la.snapshot()["process"]["accounted_cpu_s"] < 0.04
    finally:
        loop.close()


def test_callback_exception_and_cancelled_handle_behavior(monkeypatch):
    from hermes_cli import loop_callback_latency as latency
    monkeypatch.setattr(latency, "THRESHOLD_S", 0.0)
    loop = asyncio.new_event_loop()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context["exception"]))
    error = ValueError("secret-error")
    def fail():
        raise error
    la.instrument_callbacks()
    try:
        asyncio.Handle(fail, (), loop)._run()
        assert errors == [error]
        handle = loop.call_soon(fail)
        handle.cancel()
        loop.call_soon(loop.stop)
        loop.run_forever()
        assert errors == [error]
        assert "secret-error" not in json.dumps(latency.snapshot())
    finally:
        loop.close()


def test_bounds_overflow_safe_code_identity_and_snapshot_copy(monkeypatch):
    from hermes_cli import loop_callback_latency as latency
    monkeypatch.setattr(latency, "THRESHOLD_S", 0.0)
    for i in range(100):
        fn = type(blocked)(blocked.__code__.replace(co_firstlineno=i+1), globals())
        latency.record(fn, 0.6, 0.01, 1000.0)
    snap = latency.snapshot()
    assert len(snap["identities"]) <= 65
    assert len(snap["recent"]) == 64
    assert snap["records_dropped"] == 36
    assert snap["identity_overflow"] == 36
    assert snap["recent"][-1]["identity"] == "other"
    snap["recent"].clear()
    assert len(latency.snapshot()["recent"]) == 64
    latency.reset()
    fn = type(blocked)(blocked.__code__.replace(co_filename="/private/secret-user/file.py"), globals())
    latency.record(functools.partial(fn, "secret-arg"), 0.6, 0.01, 1000.0)
    assert latency.snapshot()["recent"][0]["identity"] == "other"
    assert "secret" not in json.dumps(latency.snapshot())


def test_task_name_and_callable_repr_are_not_identity(monkeypatch):
    from hermes_cli import loop_callback_latency as latency
    monkeypatch.setattr(latency, "THRESHOLD_S", 0.0)
    class Hostile:
        def __repr__(self):
            raise AssertionError("repr called")
        def __call__(self):
            pass
    latency.record(Hostile(), 0.6, 0.01, 1000.0)
    assert latency.snapshot()["recent"][0]["identity"] == "other"
    la.instrument_callbacks()
    async def coro():
        pass
    async def main():
        await asyncio.create_task(coro(), name="secret-task-name")
    asyncio.run(main())
    text = json.dumps(latency.snapshot())
    assert "secret-task-name" not in text
    assert "coro" in text


def test_snapshot_has_http_and_source_provenance():
    snap = la.snapshot()
    assert snap["http"]["unit"] == "ASGI body bytes"
    assert snap["diagnostic_code"]["sha256"]
    assert snap["diagnostic_code"]["files"]
    assert snap["slow_callbacks"]["emission"] == "existing snapshot cadence only"


def test_threshold_boundary_and_fork_reset():
    from hermes_cli import loop_callback_latency as latency
    latency.record(blocked, 0.499999, 0.01, 1000.0)
    assert latency.snapshot()["slow_callbacks"] == 0
    latency.record(blocked, 0.5, 0.01, 1000.0)
    assert latency.snapshot()["slow_callbacks"] == 1
    latency._after_fork_child()
    assert latency.snapshot()["slow_callbacks"] == 0
    assert latency.snapshot()["identities"] == {}


def test_system_exit_callback_still_propagates(monkeypatch):
    from hermes_cli import loop_callback_latency as latency
    monkeypatch.setattr(latency, "THRESHOLD_S", 0.0)
    loop = asyncio.new_event_loop()
    def terminate():
        raise SystemExit(17)
    la.instrument_callbacks()
    try:
        with pytest.raises(SystemExit) as exc:
            asyncio.Handle(terminate, (), loop)._run()
        assert exc.value.code == 17
        assert latency.snapshot()["slow_callbacks"] == 1
    finally:
        loop.close()
