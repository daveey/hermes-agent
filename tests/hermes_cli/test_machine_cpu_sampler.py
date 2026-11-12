"""The hub CPU advisory sampler: its own cadence, at-most-once report hand-off, and fail-closed lifetime."""
import logging
import threading

import pytest

from hermes_cli import machine_cpu_metrics as cpu
from hermes_cli import machine_cpu_sampler as cs
from hermes_cli import machine_metrics as mm

CPU = dict(cpu_busy_pct=12.5, cpu_busy_window_ms=1000, cpu_busy_sample_end_ms=1_700_000_000_000)


def _sampler(now, gate=None, samples=None, recorded=None):
    gate = gate if gate is not None else [(cpu.ON, 50.0)]
    samples = samples if samples is not None else []
    recorded = recorded if recorded is not None else []

    def sample():
        return samples.pop(0) if samples else dict(CPU)
    return cs.AdvisorySampler(limit_fn=lambda: gate[0], sample_fn=sample, record_fn=recorded.append,
                              clock=lambda: now[0])


def test_each_valid_sample_is_recorded_and_reported_at_most_once():
    now, recorded = [100.0], []
    s = _sampler(now, recorded=recorded)
    assert s.run_once() == CPU and recorded == [CPU] and s.records == 1
    assert s.take(50.0) == CPU
    assert s.take(50.0) is None  # never replayed into a second report
    second = dict(CPU, cpu_busy_sample_end_ms=CPU["cpu_busy_sample_end_ms"] + 5000)
    s.sample_fn = lambda: dict(second)
    s.run_once()
    assert s.take(50.0) == second


def test_take_refuses_a_stale_sample_or_one_measured_under_another_limit():
    now = [100.0]
    s = _sampler(now)
    s.run_once()
    assert s.take(60.0) is None  # the limit changed since the measurement
    now[0] += cs.REPORT_MAX_AGE_S + 0.001
    assert s.take(50.0) is None
    now[0] = 100.0 + cs.REPORT_MAX_AGE_S
    assert s.take(50.0) == CPU  # the age bound is inclusive


@pytest.mark.parametrize("bad", [None, {}, {"cpu_busy_pct": 1.0, "cpu_busy_window_ms": 1000}])
def test_a_dropped_sample_is_counted_logged_and_never_recorded(caplog, bad):
    now, recorded = [100.0], []
    s = _sampler(now, samples=[bad, bad], recorded=recorded)
    with caplog.at_level(logging.INFO, logger=cs.__name__):
        assert s.run_once() is None
        assert s.run_once() is None
    assert recorded == [] and s.drops == 2 and s.consecutive_drops == 2
    assert s.take(50.0) is None
    assert any("cpu_sample_drops=2 (consecutive=2" in m for m in caplog.messages)
    assert cs.status()["consecutive_drops"] == 2
    s.run_once()  # a valid sample ends the run of drops
    assert s.consecutive_drops == 0 and recorded == [CPU] and cs.status()["consecutive_drops"] == 0


@pytest.mark.parametrize("gate", [(cpu.OFF, None), (cpu.UNKNOWN, None)])
def test_gate_not_on_samples_nothing_and_forgets_the_last_sample(gate):
    now, recorded, calls = [100.0], [], []
    g = [(cpu.ON, 50.0)]
    s = _sampler(now, gate=g, recorded=recorded)
    s.run_once()
    g[0] = gate
    s.sample_fn = lambda: calls.append(1)
    assert s.run_once() is None and calls == [] and s.drops == 0
    assert s.take(50.0) is None


def test_record_failure_still_hands_the_sample_to_the_report():
    now = [100.0]
    s = _sampler(now)

    def fail(_sample):
        raise OSError("read-only cache")
    s.record_fn = fail
    assert s.run_once() == CPU and s.take(50.0) == CPU


def test_default_record_writes_the_advisory_file(tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *a, **k: tmp_path)
    s = cs.AdvisorySampler(limit_fn=lambda: (cpu.ON, 50.0), sample_fn=lambda: dict(CPU))
    s.run_once()
    assert mm.read_hub_cpu_sample(tmp_path) == CPU


def test_thread_keeps_its_own_cadence_and_stops_on_close():
    rounds = threading.Semaphore(0)
    s = cs.AdvisorySampler(limit_fn=lambda: (cpu.ON, 50.0), every_s=0.01,
                           sample_fn=lambda: rounds.release() or dict(CPU), record_fn=lambda _s: None)
    thread = s.start()
    assert thread.daemon and thread.name == "hub-cpu-advisory-sampler"
    for _ in range(3):  # several rounds without any caller driving it
        assert rounds.acquire(timeout=5)
    s.close()
    assert not thread.is_alive()
    idle = cs.AdvisorySampler(limit_fn=lambda: (cpu.OFF, None))  # mid-wait on the default 5 s cadence
    t2 = idle.start()
    idle.close()
    assert not t2.is_alive()  # close wakes the wait at once


def test_a_failing_round_does_not_end_the_thread():
    rounds = threading.Semaphore(0)

    def sample():
        rounds.release()
        raise RuntimeError("controlled")
    s = cs.AdvisorySampler(limit_fn=lambda: (cpu.ON, 50.0), every_s=0.01, sample_fn=sample)
    s.start()
    assert rounds.acquire(timeout=5) and rounds.acquire(timeout=5)
    s.close()


def test_default_cadence_is_five_windows():
    assert cs.sample_every_s() == 5 * cpu.WINDOW_S == 5.0
