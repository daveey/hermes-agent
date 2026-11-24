"""Always-on report diagnostics, not opt-in timing spans. [daveey]

``sync.detail/<step>.due`` is the not-due decision, including a lock miss.
``collect`` times ``collect()`` only. ``send`` times the send callable only.
All three are ``outer=False`` and must not change sender results or errors.
"""
import threading

import pytest

from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_cpu as sync_cpu
from hermes_cli import kanban_sync_timing as timing
from hermes_cli import loop_accounting
from hermes_cli import machine_cpu_metrics as cpu
from hermes_cli import machine_metrics as mm


def report(reporter):
    """Production steps ``sync_timing.report``; this name books the same step."""
    return reporter.report_due()


@pytest.fixture
def accounting(monkeypatch):
    loop_accounting.reset()
    monkeypatch.setattr(sync_cpu, "_local", threading.local())
    monkeypatch.setattr(loop_accounting, "scopes_enabled", lambda: False)
    yield
    loop_accounting.reset()


def _spy(monkeypatch):
    real = loop_accounting.record
    details = []

    def record(name, **kwargs):
        if name.startswith("sync.detail/"):
            details.append((name, kwargs.get("outer", True)))
        return real(name, **kwargs)

    monkeypatch.setattr(loop_accounting, "record", record)
    return details


def _spans(monkeypatch):
    spans = []
    real = timing.span

    def span(name):
        spans.append(name)
        return real(name)

    monkeypatch.setattr(timing, "span", span)
    return spans


def test_lock_miss_records_only_due(accounting, monkeypatch):
    details = _spy(monkeypatch)
    collected, sent = [], []
    reporter = mm.Reporter(
        lambda sample: sent.append(sample),
        collect_fn=lambda: collected.append(1) or {"cpus": 1},
        clock=lambda: 0.0,
    )
    release, held = threading.Event(), threading.Event()

    def hold():
        reporter._lock.acquire()
        held.set()
        release.wait(5)
        reporter._lock.release()

    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(5)
    try:
        assert sync_cpu.step(report, reporter) is False
    finally:
        release.set()
        thread.join(5)
    assert collected == [] and sent == []
    assert details == [("sync.detail/report.due", False)]
    assert sum(loop_accounting._copy_accounted().values()) == 0


def test_clock_not_due_records_only_due_and_releases_the_lock(accounting, monkeypatch):
    details = _spy(monkeypatch)
    collected = []
    reporter = mm.Reporter(
        lambda sample: None,
        collect_fn=lambda: collected.append(1) or {"cpus": 1},
        clock=lambda: 1.0,
    )
    reporter.next_at = 100.0
    assert sync_cpu.step(report, reporter) is False
    assert collected == []
    assert details == [("sync.detail/report.due", False)]
    assert reporter._lock.acquire(blocking=False)
    reporter._lock.release()


def test_due_report_times_collect_and_send_only(accounting, monkeypatch):
    now = [20.0]
    monkeypatch.setattr(sync_cpu.time, "thread_time", lambda: now[0])
    details = _spy(monkeypatch)
    spans = _spans(monkeypatch)
    sent = []
    reporter_box = []

    def collect():
        now[0] += 0.4
        return {"cpus": 3, "claiming": 0}

    def limit():
        now[0] += 0.9
        return (cpu.OFF, None)

    def send(sample):
        assert not reporter_box[0]._lock.acquire(blocking=False)
        now[0] += 0.2
        sent.append(dict(sample))

    reporter = mm.Reporter(send, collect_fn=collect, clock=lambda: 5.0, cpu_limit_fn=limit)
    reporter_box.append(reporter)
    assert sync_cpu.step(report, reporter) is True
    assert sent == [{"cpus": 3, "claiming": 0}]
    assert spans == ["collect", "limit", "send"]
    assert "due" not in spans
    assert details == [
        ("sync.detail/report.due", False),
        ("sync.detail/report.collect", False),
        ("sync.detail/report.send", False),
    ]
    stats = loop_accounting.counters()
    assert stats["sync.detail/report.collect"]["cpu_s"] == pytest.approx(0.4)
    assert stats["sync.detail/report.send"]["cpu_s"] == pytest.approx(0.2)
    assert stats["sync.detail/report.due"]["cpu_s"] == pytest.approx(0.0)
    assert sum(loop_accounting._copy_accounted().values()) == 0
    assert reporter._lock.acquire(blocking=False)
    reporter._lock.release()


def test_send_error_is_recorded_and_not_raised(accounting, monkeypatch):
    details = _spy(monkeypatch)

    def send(_sample):
        raise OSError("network down")

    reporter = mm.Reporter(send, collect_fn=lambda: {"cpus": 1}, clock=lambda: 3.0)
    assert sync_cpu.step(report, reporter) is False
    assert reporter.next_at == 3.0 + mm.REPORT_EVERY_S
    assert ("sync.detail/report.send", False) in details
    assert ("sync.detail/report.collect", False) in details
    assert reporter._lock.acquire(blocking=False)
    reporter._lock.release()


def test_old_board_backoff_still_records_send(accounting, monkeypatch):
    details = _spy(monkeypatch)

    def send(_sample):
        raise ksc.HubError(404, "not found")

    reporter = mm.Reporter(send, collect_fn=lambda: {"cpus": 1}, clock=lambda: 8.0)
    assert sync_cpu.step(report, reporter) is False
    assert reporter.next_at == 8.0 + mm.UNSUPPORTED_RETRY_S
    assert ("sync.detail/report.send", False) in details


def test_collect_error_records_collect_and_skips_send(accounting, monkeypatch):
    details = _spy(monkeypatch)
    sent = []

    def collect():
        raise RuntimeError("census")

    reporter = mm.Reporter(lambda sample: sent.append(sample), collect_fn=collect, clock=lambda: 2.0)
    assert sync_cpu.step(report, reporter) is False
    assert sent == []
    assert reporter.next_at == 2.0 + mm.REPORT_EVERY_S
    names = [name for name, _outer in details]
    assert names.count("sync.detail/report.collect") == 1
    assert "sync.detail/report.send" not in names


def test_withheld_report_collects_without_sending(accounting, monkeypatch):
    details = _spy(monkeypatch)
    sent = []
    reporter = mm.Reporter(
        lambda sample: sent.append(sample),
        collect_fn=lambda: {"cpus": 1},
        clock=lambda: 100.0,
        cpu_limit_fn=lambda: (cpu.ON, 50.0),
        cpu_take_fn=lambda limit: None,
    )
    reporter.sent_at = 99.0
    assert sync_cpu.step(report, reporter) is False
    assert sent == [] and reporter.suppressed == 1
    names = [name for name, _outer in details]
    assert "sync.detail/report.due" in names
    assert "sync.detail/report.collect" in names
    assert "sync.detail/report.send" not in names


def test_downgrade_retry_times_each_send_call(accounting, monkeypatch):
    details = _spy(monkeypatch)
    calls = []

    def send(sample):
        calls.append("claiming" in sample)
        if "claiming" in sample:
            raise ksc.HubError(400, "unknown field(s): claiming")
        return {}

    reporter = mm.Reporter(
        send,
        collect_fn=lambda: {"cpus": 1, "claiming": 1},
        clock=lambda: 1.0,
        cpu_limit_fn=lambda: (cpu.OFF, None),
    )
    assert sync_cpu.step(report, reporter) is True
    assert calls == [True, False]
    sends = [item for item in details if item[0] == "sync.detail/report.send"]
    assert sends == [("sync.detail/report.send", False), ("sync.detail/report.send", False)]
    assert reporter.claiming_off_until == 1.0 + mm.UNSUPPORTED_RETRY_S


def test_report_tick_skip_is_not_a_due_detail(accounting, monkeypatch):
    details = _spy(monkeypatch)
    reporter = mm.Reporter(lambda sample: None, collect_fn=lambda: {"cpus": 1}, clock=lambda: 0.0)
    reporter.next_at = 1000.0
    assert reporter.report_tick(lambda sample: None) is None
    assert details == []
