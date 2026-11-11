"""Split-phase CPU reporting, tracer slice 1: the begin call on a gated host.

On a gated (root limit ON) host the reporter's due call must not block for the ~1 s CPU window on the sync-loop
thread: it takes the first same-thread snapshot, advances ``next_at`` once, sends nothing and returns False (the
loop's "not sent" result).  The legacy synchronous ``cpu_sample_fn`` is the blocking primitive here: it advances the
fake clock by the window, so any call to it on the begin call is the failure this slice forbids.  A gate that is
OFF keeps the original single-phase report.
"""
from hermes_cli import machine_cpu_metrics as cpu
from hermes_cli import machine_metrics as mm

CPU = {"cpu_busy_pct": 12.5, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": 1_700_000_000_000}


def _reporter(limit, now, sent, blocked):
    def sample(*args, **kwargs):  # a blocking finish stand-in must never run at begin
        blocked.append(now[0])
        now[0] += 1.0
        return dict(CPU)
    return mm.Reporter(sent.append, collect_fn=lambda: {"cpus": 4}, clock=lambda: now[0],
                       cpu_limit_fn=lambda: limit, cpu_begin_fn=lambda **kw: (now[0], None),
                       cpu_finish_fn=sample)


def test_gated_begin_neither_blocks_nor_sends_and_leaves_one_pending_wake():
    now, sent, blocked = [1000.0], [], []
    reporter = _reporter((cpu.ON, 50.0), now, sent, blocked)

    due = reporter.report_due()

    assert blocked == [], "begin must not run the ~1 s blocking CPU sample on the loop thread"
    assert now[0] == 1000.0, "begin consumed loop time"
    assert sent == [], "begin must not send"
    assert due is False
    assert reporter.next_at == 1000.0 + mm.REPORT_EVERY_S  # the 10 s schedule advances at begin
    wake = reporter.pending_wake_in()  # the loop clamps its wait to this: positive, never a zero-wait spin
    assert wake is not None and 0.0 < wake <= cpu.MIN_WINDOW_MS / 1000.0


def test_off_gate_keeps_the_original_single_phase_report():
    now, sent, blocked = [1000.0], [], []
    reporter = _reporter((cpu.OFF, None), now, sent, blocked)

    assert reporter.report_due() is True

    assert sent == [{"cpus": 4}] and blocked == []  # one call, no CPU snapshot, no pending state
    assert reporter.next_at == 1000.0 + mm.REPORT_EVERY_S
