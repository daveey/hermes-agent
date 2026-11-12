"""A missing CPU triple is observable quality data, never an admission sample."""
import logging

import pytest

from hermes_cli import machine_cpu_metrics as cpu
from hermes_cli import machine_metrics as mm


def test_drops_report_cumulative_quality_without_changing_wire_sample(caplog):
    now, sent = [100.0], []
    rep = mm.Reporter(sent.append, clock=lambda: now[0],
                      collect_fn=lambda: {'cpus': 4},
                      cpu_limit_fn=lambda: (cpu.ON, 50.0),
                      cpu_begin_fn=lambda **kw: (now[0], (100, 50)),
                      cpu_finish_fn=lambda *a, **kw: None)
    with caplog.at_level(logging.INFO, logger=mm.__name__):
        for start in (100.0, 110.0):
            now[0] = start
            assert rep.report_due() is False
            now[0] += 3.0
            assert rep.report_due() is True
            assert rep.pending_wake_in() is None
            assert rep.report_due() is False
    assert sent == [{'cpus': 4}, {'cpus': 4}]
    assert [r.getMessage() for r in caplog.records if r.name == mm.__name__] == [
        'machine metrics admission quality: cpu_sample_drops=1',
        'machine metrics admission quality: cpu_sample_drops=2',
    ]
    assert rep.cpu_sample_drops == 2


def test_drop_quality_survives_send_failure(caplog):
    now = [100.0]
    def fail_send(sample):
        raise OSError('controlled transport failure')
    rep = mm.Reporter(fail_send, clock=lambda: now[0], collect_fn=dict,
                      cpu_limit_fn=lambda: (cpu.ON, 50.0),
                      cpu_begin_fn=lambda **kw: (now[0], (100, 50)),
                      cpu_finish_fn=lambda *a, **kw: None)
    with caplog.at_level(logging.INFO, logger=mm.__name__):
        assert rep.report_due() is False
        now[0] += 3
        assert rep.report_due() is False
    assert rep.pending_wake_in() is None
    assert 'machine metrics admission quality: cpu_sample_drops=1' in caplog.messages


@pytest.mark.parametrize('failure_site', ['limit', 'collect', 'finish'])
def test_exceptions_before_cpu_decision_are_not_counted_as_drops(caplog, failure_site):
    now, sent, reached = [100.0], [], []

    def controlled(site, result):
        reached.append(site)
        if now[0] > 100.0 and failure_site == site:
            raise OSError('controlled excluded exception')
        return result

    rep = mm.Reporter(sent.append, clock=lambda: now[0],
                      collect_fn=lambda: controlled('collect', {'cpus': 4}),
                      cpu_limit_fn=lambda: controlled('limit', (cpu.ON, 50.0)),
                      cpu_begin_fn=lambda **kw: (now[0], (100, 50)),
                      cpu_finish_fn=lambda *a, **kw: controlled('finish', None))
    with caplog.at_level(logging.INFO, logger=mm.__name__):
        assert rep.report_due() is False
        assert rep.pending_wake_in() == cpu.WINDOW_S
        reached.clear()
        now[0] += cpu.WINDOW_S
        assert rep.report_due() is False
    assert reached[-1] == failure_site
    assert rep.pending_wake_in() is None
    assert rep.cpu_sample_drops == 0
    assert sent == []
    assert not [r for r in caplog.records if r.name == mm.__name__]


def test_successful_sample_and_off_report_do_not_log_drops(caplog, monkeypatch):
    now, sent = [100.0], []
    gate: list[tuple[str, float | None]] = [(cpu.ON, 50.0)]
    triple = dict(cpu_busy_pct=12.5, cpu_busy_window_ms=1000, cpu_busy_sample_end_ms=1700000000000)
    monkeypatch.setattr(mm, 'record_hub_cpu_sample', lambda sample: None)
    rep = mm.Reporter(sent.append, clock=lambda: now[0], collect_fn=lambda: {'cpus': 4},
                      cpu_limit_fn=lambda: gate[0],
                      cpu_begin_fn=lambda **kw: (now[0], (100, 50)),
                      cpu_finish_fn=lambda *a, **kw: dict(triple))
    with caplog.at_level(logging.INFO, logger=mm.__name__):
        assert rep.report_due() is False
        now[0] += 1
        assert rep.report_due() is True
        now[0] = 110
        gate[0] = (cpu.OFF, None)
        assert rep.report_due() is True
    assert sent == [dict(cpus=4, **triple), {'cpus': 4}]
    assert rep.cpu_sample_drops == 0
    assert not [r for r in caplog.records if r.name == mm.__name__]
