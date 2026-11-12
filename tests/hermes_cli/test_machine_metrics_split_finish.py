"""One pending CPU sample is consumed once, even when a finish fails or the gate changes."""
import pytest
from hermes_cli import machine_cpu_metrics as cpu
from hermes_cli import machine_metrics as mm

CPU = dict(cpu_busy_pct=12.5, cpu_busy_window_ms=1000, cpu_busy_sample_end_ms=1700000000000)

@pytest.fixture
def harness(monkeypatch):
    now, gate, sent, finishes = [100.0], [(cpu.ON, 50.0)], [], []
    monkeypatch.setattr(cpu, 'begin', lambda **kw: (now[0], (100, 50)))
    def finish(first, **kw):
        finishes.append(first)
        return dict(CPU) if 1 <= now[0]-first[0] <= 2 else None
    monkeypatch.setattr(cpu, 'finish', finish)
    monkeypatch.setattr(mm, 'record_hub_cpu_sample', lambda sample: None)
    rep = mm.Reporter(sent.append, clock=lambda: now[0], collect_fn=lambda: {'cpus': 4},
                      cpu_limit_fn=lambda: gate[0])
    return rep, now, gate, sent, finishes

@pytest.mark.parametrize('elapsed', [1.0, 2.0, 2.001, 30.0])
def test_finish_once_schedule_unchanged_and_late_sample_dropped(harness, elapsed):
    rep, now, _gate, sent, finishes = harness
    assert rep.report_due() is False
    assert rep.next_at == 110
    now[0] += 0.5
    assert rep.report_due() is False and rep.pending_wake_in() == 0.5
    now[0] = 100 + elapsed
    assert rep.report_due() is True
    assert sent == [dict(cpus=4, **CPU) if elapsed <= 2 else {'cpus': 4}]
    assert len(finishes) == 1 and rep.pending_wake_in() is None
    assert rep.next_at == 110
    assert rep.cpu_sample_drops == (0 if elapsed <= 2 else 1)
    if elapsed < 10:
        assert rep.report_due() is False and len(sent) == 1

@pytest.mark.parametrize('new_gate', [(cpu.OFF, None), (cpu.UNKNOWN, None), (cpu.ON, 60.0)])
def test_limit_rechecked_and_changed_snapshot_discarded(harness, new_gate):
    rep, now, gate, sent, finishes = harness
    rep.report_due()
    gate[0] = new_gate
    now[0] += 1
    assert rep.report_due() is True
    assert sent == [{'cpus': 4}] and finishes == []
    assert rep.pending_wake_in() is None and rep.cpu_sample_drops == 1

@pytest.mark.parametrize('where', ['finish', 'limit', 'send'])
def test_finish_exception_never_leaves_due_pending(harness, monkeypatch, where):
    rep, now, _gate, _sent, _finishes = harness
    rep.report_due()
    def fail(*a, **kw):
        raise RuntimeError('controlled failure')
    if where == 'finish':
        monkeypatch.setattr(cpu, 'finish', fail)
    elif where == 'limit':
        rep.cpu_limit_fn = fail
    else:
        rep.send = fail
    now[0] += 1
    assert rep.report_due() is False
    assert rep.pending_wake_in() is None and rep.next_at == 110

@pytest.mark.parametrize('status', [404, 405])
def test_unsupported_finish_uses_600s_retry(harness, status):
    rep, now, _gate, _sent, _finishes = harness
    class Unsupported(Exception):
        pass
    def fail(sample):
        exc = Unsupported()
        exc.status = status
        raise exc
    rep.send = fail
    rep.report_due()
    now[0] += 1
    assert rep.report_due() is False
    assert rep.next_at == 701 and rep.pending_wake_in() is None


def test_pending_wake_and_finish_use_same_window(harness, monkeypatch):
    rep, now, _gate, _sent, _finishes = harness
    monkeypatch.setattr(cpu, 'WINDOW_S', 0.5)
    assert rep.report_due() is False
    now[0] += 0.5
    assert rep.pending_wake_in() == 0
    assert rep.report_due() is True
    assert rep.pending_wake_in() is None


def test_slow_begin_cannot_return_a_due_pending_snapshot(harness):
    rep, now, _gate, sent, _finishes = harness
    def slow_begin(**kwargs):
        first = (now[0], (100, 50))
        now[0] += 1.25
        return first
    rep.cpu_begin_fn = slow_begin
    assert rep.report_due() is True
    assert rep.pending_wake_in() is None and len(sent) == 1
