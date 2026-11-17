"""Deterministic same-thread CPU partition, without network or live captures."""
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_sync_cpu as cpu
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_timing as timing


@pytest.fixture
def measured(monkeypatch):
    clock = [0.0]
    records = []
    monkeypatch.setattr(cpu.time, "thread_time", lambda: clock[0])
    monkeypatch.setattr(cpu.loop_accounting, "record", lambda name, **kw: records.append((name, kw)))
    monkeypatch.setattr(cpu, "_local", threading.local())
    return clock, records


def values(records, name):
    return {k.split('/')[0]: v["cpu_s"] for k, v in records if k.endswith('/' + name)}


def test_nested_services_and_calls_partition_once(measured):
    clock, records = measured

    @cpu.board_call
    def inner():
        clock[0] += 3

    @cpu.board_call
    def outer():
        clock[0] += 2
        inner()

    with cpu.service("loop"):
        clock[0] += 1
        with cpu.service("board/test"):
            clock[0] += 4
            outer()
        clock[0] += 2
    assert values(records, "board/test") == {
        "sync.cpu.local": 4, "sync.cpu.board_call": 5, "sync.cpu.total": 9}
    assert values(records, "loop") == {
        "sync.cpu.local": 7, "sync.cpu.board_call": 5, "sync.cpu.total": 12}
    assert all(not r["outer"] for _, r in records)


def test_exception_publishes_and_restores(measured):
    clock, records = measured

    @cpu.board_call
    def fail():
        clock[0] += 3
        raise ValueError("test")

    with pytest.raises(ValueError, match="test"), cpu.service("failed"):
        clock[0] += 2
        fail()
    assert values(records, "failed")["sync.cpu.local"] == 2
    assert all(r["error_kind"] == "ValueError" for _, r in records)
    assert cpu._local.frames == ()
    assert not cpu._local.in_call


@pytest.mark.parametrize("proxied", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_real_hub_call_routes_include_serialization_and_errors(measured, monkeypatch, proxied, failed):
    clock, records = measured
    hub = client.Hub(client.Identity("http://example.invalid", "test", "key"))
    monkeypatch.setattr(hub, "_proxied", lambda parts: proxied)
    original = client.json.dumps

    def encode(body):
        clock[0] += 2
        return original(body)

    def transport(*args, **kwargs):
        clock[0] += 3
        if failed:
            raise OSError("test transport")
        return {"ok": True}

    monkeypatch.setattr(client.json, "dumps", encode)
    monkeypatch.setattr(hub, "_call_urlopen" if proxied else "_call_kept_alive", transport)
    with cpu.service("hub"):
        clock[0] += 1
        if failed:
            with pytest.raises(OSError, match="test transport"):
                hub._call("POST", "/test", {"payload": 1})
        else:
            assert hub._call("POST", "/test", {"payload": 1}) == {"ok": True}
    assert values(records, "hub") == {
        "sync.cpu.local": 1, "sync.cpu.board_call": 5, "sync.cpu.total": 6}


def test_unscoped_and_other_thread_calls_are_not_attributed(measured):
    clock, records = measured

    @cpu.board_call
    def remote():
        return bool(getattr(cpu._local, "in_call", False))

    assert remote() is False
    outcomes = []
    with cpu.service("owner"):
        thread = threading.Thread(target=lambda: outcomes.append(remote()))
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
        clock[0] += 4
    assert outcomes == [False]
    assert values(records, "owner")["sync.cpu.board_call"] == 0
    assert values(records, "owner")["sync.cpu.local"] == 4


def test_board_facade_records_local_work_and_restores(measured, monkeypatch):
    clock, records = measured
    monkeypatch.setattr(timing._DIAG, "board_start", lambda *args: None)
    monkeypatch.setattr(timing._DIAG, "board_end", lambda *args: None)
    results = []
    mark = timing.board_start("test", results)
    clock[0] += 4
    results.append(SimpleNamespace(pulled=1, skipped=False, error=None))
    timing.board_end(mark, results)
    assert values(records, "board/test")["sync.cpu.local"] == 4
    assert cpu._local.frames == ()


def test_iteration_includes_wait_cpu_and_closes_on_early_return(measured):
    clock, records = measured
    rounds = cpu.iterations(lambda: True)
    try:
        next(rounds)
        clock[0] += 5  # local work, including CPU consumed by wait_for_work
    finally:
        rounds.close()
    assert values(records, "loop")["sync.cpu.local"] == 5
    assert cpu._local.frames == ()


def test_inflight_service_is_not_published_until_complete(measured):
    clock, records = measured
    with cpu.service("test"):
        clock[0] += 7
        assert records == []
    assert values(records, "test")["sync.cpu.total"] == 7


def test_prefork_service_does_not_publish_mixed_clock(measured, monkeypatch):
    clock, records = measured
    pid = [1]
    monkeypatch.setattr(cpu.os, "getpid", lambda: pid[0])
    with cpu.service("fork"):
        clock[0] += 2
        pid[0] = 2
    assert records == []
    assert cpu._local.frames == ()
