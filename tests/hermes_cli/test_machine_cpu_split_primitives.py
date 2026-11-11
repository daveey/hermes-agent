"""Nonblocking primitives preserve the locked sampler's arithmetic and window contract."""
import pytest
from hermes_cli import machine_cpu_metrics as cpu

A = "cpu 10 20 30 40 10 5 5 0 999 999"
B = "cpu 20 40 40 80 20 10 10 10 999 999"

@pytest.mark.parametrize("elapsed,valid", [(0.999, False), (1.0, True), (2.0, True), (2.001, False)])
def test_begin_finish_no_sleep_and_same_busy_math(elapsed, valid):
    now = [100.0]
    first = cpu.begin(read=lambda: A, clock=lambda: now[0], linux=True)
    assert now == [100.0]
    now[0] += elapsed
    result = cpu.finish(first, read=lambda: B, clock=lambda: now[0], wall=lambda: 1700000000.0)
    if valid:
        assert result == dict(cpu_busy_pct=cpu.busy_pct(cpu.parse_proc_stat(A), cpu.parse_proc_stat(B)),
                              cpu_busy_window_ms=round(elapsed*1000), cpu_busy_sample_end_ms=1700000000000)
    else:
        assert result is None

@pytest.mark.parametrize("text", ["bad", "cpu 1 2 3", "cpu -1 2 3 4 5 6 7 8"])
def test_bad_first_snapshot_is_unknown(text):
    first = cpu.begin(read=lambda: text, clock=lambda: 0.0, linux=True)
    assert cpu.finish(first, read=lambda: B, clock=lambda: 1.0) is None
