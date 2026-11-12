"""Hub metrics sender across process stalls and gateway restarts. [daveey]

Regressions (hermes-hub, 2026-11-12, PR #1429 live probe):

- 13:41:46-13:42:11 the whole gateway process stalled (control event loop blocked 24.4 s, the advisory sampler's
  window 25.3 s, a 25.2 s ``worker.persist``); the sender's 13:41:48 tick finished at 13:42:12 and sent the hub row
  WITHOUT the CPU triple (``end_ms=None``) because the sampler's next sample was still ~0.3 s away. The same
  happened at 13:43:27. A CPU-less report erases the row's triple.
- After that late tick (send at 13:42:47.9) the next anchor (13:42:48.4) was skipped by the half-period guard
  (``skipped=1``), losing a report the guard only needed to delay.
- Each hub gateway restart: the new root gateway's first ``.sync.lock`` attempt found a profile gateway holding it and
  retried only every 30 s (contention 30-31 s on all 7 restarts; see tests/gateway/test_kanban_singleton_lease.py).
"""
from __future__ import annotations

import itertools
import logging
import threading
import time

import pytest

from hermes_cli import machine_cpu_metrics as cpu
from hermes_cli import machine_cpu_sampler as cs
from hermes_cli import machine_metrics as mm
from hermes_cli import machine_metrics_sender as ms

P = mm.REPORT_EVERY_S
SAMPLE = {"cpu_busy_pct": 12.0, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": 1}
ON = lambda: (cpu.ON, 50.0)


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _reporter(clock, sends, *, take, wait=None, collect=lambda: {"cpus": 2}):
    reporter = mm.Reporter(lambda s: sends.append(("loop", clock.now, dict(s))), collect_fn=collect, clock=clock,
                           cpu_limit_fn=ON, cpu_take_fn=take)
    reporter.cpu_wait_fn = wait
    return reporter


def test_a_tick_that_finds_no_fresh_sample_waits_for_the_samplers_next_one():
    """13:42:12: the tick ran as the stall ended, 0.3 s before the sampler's next valid window closed."""
    clock, sends, pending = FakeClock(100.0), [], []
    waits = []

    def take(_limit):
        return pending.pop() if pending else None

    def wait(timeout):  # the sampler publishes its next sample 0.3 s into the wait
        waits.append(timeout)
        clock.now += 0.3
        pending.append(dict(SAMPLE))
        return True
    reporter = _reporter(clock, sends, take=take, wait=wait)
    assert reporter.report_tick(lambda s: sends.append(("sender", clock.now, dict(s)))) is True
    (who, _at, body), = sends
    assert who == "sender" and set(SAMPLE) <= set(body)  # CPU-bearing, not end_ms=None
    assert waits and waits[0] <= mm.CPU_WAIT_S


def test_the_wait_for_a_sample_is_bounded():
    clock, sends = FakeClock(100.0), []

    def wait(timeout):
        clock.now += timeout  # nothing published
        return False
    reporter = _reporter(clock, sends, take=lambda _l: None, wait=wait)
    reporter.sent_at = clock.now - 1.0  # a report just went out
    t0 = clock.now
    assert reporter.report_tick(lambda s: sends.append(s)) is None
    assert clock.now - t0 <= mm.CPU_WAIT_S + 1e-9


@pytest.mark.parametrize("path", ["sender", "loop"])
def test_a_cpu_less_report_is_suppressed_while_the_gate_is_on_and_a_report_went_out_recently(path):
    """13:42:12 / 13:43:27: a report without the triple overwrote the row's CPU fields. While the root gate is ON a
    CPU-less report goes out only as a keepalive, when nothing was sent for KEEPALIVE_S."""
    clock, sends = FakeClock(100.0), []
    reporter = _reporter(clock, sends, take=lambda _l: None)

    def report():
        if path == "sender":
            return reporter.report_tick(lambda s: sends.append(("sender", clock.now, dict(s))))
        reporter.next_at = 0.0  # the loop's own period never holds this test
        return reporter.report_due()
    reporter.sent_at = clock.now - (mm.KEEPALIVE_S - 1.0)
    assert not report() and sends == [] and reporter.suppressed == 1
    clock.now += 1.0  # KEEPALIVE_S since the last report: the row's non-CPU fields must not age past the Board's 90 s
    assert report() is True
    (_who, at, body), = sends
    assert at == clock.now and not set(SAMPLE) & set(body) and body["cpus"] == 2


def test_suppression_applies_only_while_the_gate_is_on():
    clock, sends = FakeClock(100.0), []
    reporter = _reporter(clock, sends, take=lambda _l: None)
    reporter.cpu_limit_fn = lambda: (cpu.OFF, None)
    reporter.sent_at = clock.now - 1.0
    assert reporter.report_tick(lambda s: sends.append(s)) is True and sends == [{"cpus": 2}]


def test_a_suppressed_report_does_not_count_as_a_report_start():
    """The half-period spacing guard measures sends; a suppressed attempt sent nothing."""
    clock, sends = FakeClock(100.0), []
    triple = []
    reporter = _reporter(clock, sends, take=lambda _l: triple.pop() if triple else None)
    reporter.sent_at = reporter.started_at = 95.0
    clock.now = 106.0
    assert reporter.report_tick(lambda s: sends.append(s)) is None
    clock.now = 107.0  # 1 s after the suppressed attempt, 12 s after the last send
    triple.append(dict(SAMPLE))
    assert reporter.report_tick(lambda s: sends.append(s)) is True


def _run(reporter, clock, *, stall_at, stall_s, until):
    """MetricsSender.run on a fake clock; the wait that covers *stall_at* returns *stall_s* late (process stall)."""
    sends, sender = [], None

    def post(sample, _probe):
        sends.append((round(clock.now, 6), set(SAMPLE) <= set(sample)))
        return {}

    def wait(seconds):
        start = clock.now
        clock.now += max(0.0, seconds)
        if start < stall_at <= clock.now:
            clock.now += stall_s
        if clock.now >= until:
            sender._stop.set()
    sender = ms.MetricsSender(reporter, post, limit_fn=ON, clock=clock, wait_fn=wait, sched_fn=lambda: None)
    sender.run()
    return sender, sends


def test_a_late_tick_delays_the_next_anchor_for_spacing_instead_of_skipping_it():
    """13:42:47.9 late send, 13:42:48.4 anchor skipped (skipped=1). The next send waits until it is spaced."""
    clock = FakeClock(0.0)
    reporter = mm.Reporter(None, collect_fn=lambda: {"cpus": 2}, clock=clock, cpu_limit_fn=ON,
                           cpu_take_fn=lambda _l: dict(SAMPLE))
    # the wait for the 20 s anchor ends 25.5 s late (a stall covering the 20/30/40 anchors)
    sender, sends = _run(reporter, clock, stall_at=20.0, stall_s=25.5, until=75.0)
    assert [t for t, _ in sends] == [10.0, 45.5, 50.5, 60.0, 70.0]
    assert sender.skipped == 0 and all(cpu_bearing for _, cpu_bearing in sends)
    gaps = [b - a for (a, _), (b, _) in itertools.pairwise(sends)]
    assert min(gaps) >= P / 2


def test_the_loop_takeover_while_on_sends_through_the_senders_bounded_transport():
    """A stalled sender hands reporting back to the loop; while the gate is ON the loop's report must not go through
    the kept-alive Hub (30 s timeout) while holding the Reporter lock: it would block the recovered sender."""
    clock, posted, hub = FakeClock(0.0), [], []
    reporter = mm.Reporter(hub.append, collect_fn=lambda: {"cpus": 2}, clock=clock, cpu_limit_fn=ON,
                           cpu_take_fn=lambda _l: dict(SAMPLE))
    sender = ms.MetricsSender(reporter, lambda s, p: posted.append(dict(s)), limit_fn=ON, clock=clock,
                              sched_fn=lambda: None)
    sender.alive = lambda: True
    sender.on, sender.tick_done_at = True, 0.0
    clock.now = ms.STALL_PERIODS * P + 1.0
    assert sender.loop_report() is True
    assert hub == [] and len(posted) == 1 and set(SAMPLE) <= set(posted[0])


def test_the_stall_warning_says_whether_a_tick_is_in_flight_not_that_the_loop_reports(caplog):
    """PR #1429 review nit: "the sync loop reports meanwhile" is false while the stalled tick holds the Reporter."""
    clock = FakeClock(0.0)
    reporter = mm.Reporter(lambda s: None, collect_fn=lambda: {"cpus": 2}, clock=clock, cpu_limit_fn=ON)
    sender = ms.MetricsSender(reporter, lambda s, p: None, limit_fn=ON, clock=clock, sched_fn=lambda: None)
    sender.alive = lambda: True
    sender.on, sender.tick_done_at, sender.tick_started_at = True, 0.0, 10.0
    clock.now = 34.0
    assert reporter._lock.acquire(blocking=False)  # the stalled tick holds the Reporter
    try:
        with caplog.at_level(logging.WARNING, logger=ms.__name__):
            assert sender.loop_report() is False
    finally:
        reporter._lock.release()
    (msg,) = [r.getMessage() for r in caplog.records if "metrics_sender stalled" in r.getMessage()]
    assert "reports meanwhile" not in msg
    assert "no tick finished for 34 s" in msg and "tick in flight for 24 s" in msg


def test_sampler_wait_fresh_wakes_on_the_next_published_sample():
    now = [100.0]
    ticks = iter(range(0, 10_000_000, 400))

    def read():
        t = next(ticks)
        return f"cpu {t // 4} 0 0 {t - t // 4} 0 0 0 0\n"
    s = cs.AdvisorySampler(limit_fn=ON, read_fn=read, record_fn=lambda _m: None, clock=lambda: now[0],
                           wall=lambda: time.time())
    assert s.run_once() is None  # primes the window
    assert s.wait_fresh(0.05) is False  # nothing new: bounded
    result = []
    waiter = threading.Thread(target=lambda: result.append(s.wait_fresh(10.0)))
    waiter.start()
    try:
        now[0] += 1.0
        assert s.run_once() is not None
    finally:
        waiter.join(10.0)
    assert result == [True]
    assert s.take(50.0) is not None


def test_a_profile_gateways_sender_reads_the_installation_root_gate(tmp_path, monkeypatch):
    """A profile gateway that holds the host sync lock (root gateway restarting) reports the hub row with the same
    root-config CPU gate, so its reports carry the triple; it never reads the profile's own config."""
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "askeve-reviewer"
    profile.mkdir(parents=True)
    (root / "config.yaml").write_text("kanban:\n  admission:\n    cpu_busy_below_pct: 50\n", encoding="utf-8")
    (profile / "config.yaml").write_text("kanban: {}\n", encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)
    assert cpu.root_limit() == (cpu.ON, 50.0)


def test_a_withheld_report_is_logged_and_counted_for_the_latency_analyzer(caplog):
    """The rollout's analyzer counts withheld ticks from their own INFO line; the send line's keys stay unchanged."""
    clock = FakeClock(100.0)
    reporter = mm.Reporter(None, collect_fn=lambda: {"cpus": 2}, clock=clock, cpu_limit_fn=ON,
                           cpu_take_fn=lambda _l: None)
    reporter.sent_at = clock.now - 1.0
    sender = ms.MetricsSender(reporter, lambda s, p: None, limit_fn=ON, clock=clock, sched_fn=lambda: None)
    with caplog.at_level(logging.INFO, logger=ms.__name__):
        assert sender.tick() is None
        sender._log_counts()
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("metrics_sender withheld t=") and "reason=no_cpu_sample" in m for m in msgs)
    assert not any(m.startswith("metrics_sender send ") for m in msgs)
    assert sender.withheld == 1 and sender.skipped == 0
    assert any(m.startswith("metrics_sender ticks=1 sent=0 failed=0 skipped=0 withheld=1") for m in msgs)
