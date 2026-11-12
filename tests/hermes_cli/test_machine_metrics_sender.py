"""Hub metrics reports from an anchored sender thread (``hermes_cli.machine_metrics_sender``). [daveey]

Regression (2026-11-12, hermes-hub): the sync loop sent the CPU-bearing metrics report once per iteration, after the
round, so a long round held every report (15-20 CPU-bearing reports per 300 s, stalls of 18-39 s). The sender ticks
on fixed anchors on its own thread while the root CPU gate is on; the loop stands down only while it does.
"""
from __future__ import annotations

import http.server
import itertools
import logging
import random
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_sync_client as client
from hermes_cli import machine_cpu_metrics as cpu
from hermes_cli import machine_metrics as mm
from hermes_cli import machine_metrics_sender as ms
from tests.hermes_cli import test_kanban_sync_timing as timing_tests
from tests.waiting import wait_until

P = mm.REPORT_EVERY_S
SAMPLE = {"cpu_busy_pct": 12.0, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": 1}


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


def _limit(state):
    return lambda: (state[0], 50.0 if state[0] == cpu.ON else None)


def _nopost(_sample, _probe):
    return {}


def test_anchors_never_drift_with_jittered_send_times():
    clock = FakeClock()
    starts: list[float] = []
    sender = None

    class Reporter:
        def report_tick(self, send):
            starts.append(clock.now)
            clock.now += (len(starts) * 7919 % 1000) / 1000 * 0.4 * P  # 0-40 % of a period, varied
            if len(starts) == 100:
                sender._stop.set()
            return True

    sender = ms.MetricsSender(Reporter(), _nopost, limit_fn=lambda: (cpu.ON, 50.0), clock=clock, wait_fn=clock.wait)
    t0 = clock.now
    sender.run()
    assert len(starts) == 100
    for k, at in enumerate(starts, start=1):
        assert at == pytest.approx(t0 + k * P, abs=1e-6), (k, at)
    assert sender.sent == 100 and sender.ticks == 100


def test_out_of_order_wake_times_lose_no_tick_with_the_real_reporter():
    """S-1: report_due's ``next_at = entry + period`` rule drops a tick that wakes earlier (relative to its anchor)
    than the one before. The sender's rule is the anchor plus the half-period guard: 100 ticks, 100 sends."""
    clock = FakeClock(0.0)
    rng = random.Random(20261112)
    lateness = [rng.uniform(0.0, 0.45 * P) for _ in range(100)]  # wake lateness, deliberately not in order
    sends: list[float] = []
    sender = None
    reporter = mm.Reporter(None, collect_fn=lambda: {"cpus": 2}, clock=clock,
                           cpu_limit_fn=lambda: (cpu.ON, 50.0), cpu_take_fn=lambda _l: dict(SAMPLE))

    def post(sample, probe):
        sends.append(clock.now)
        if len(sends) == 100:
            sender._stop.set()
        return {}

    def wait(seconds):  # the thread wakes late by a different amount each tick
        clock.now += max(0.0, seconds) + lateness[min(len(sends), 99)]
    sender = ms.MetricsSender(reporter, post, limit_fn=lambda: (cpu.ON, 50.0), clock=clock, wait_fn=wait,
                              sched_fn=lambda: None)
    sender.run()
    assert len(sends) == 100 and sender.sent == 100 and sender.skipped == 0
    assert any(b < a for a, b in itertools.pairwise(lateness))  # the wake order really was shuffled
    # The old rule on the same wake times would have dropped ticks:
    old_next, kept = 0.0, 0
    for k, late in enumerate(lateness, start=1):
        at = k * P + late
        if at >= old_next:
            old_next, kept = at + P, kept + 1
    assert kept < 100


def test_an_overrun_skips_the_covered_anchors_without_a_burst():
    clock = FakeClock()
    starts: list[float] = []
    sender = None

    class Reporter:
        def report_tick(self, send):
            starts.append(clock.now)
            if len(starts) == 2:
                clock.now += 2.5 * P  # this send overran the next two anchors
            if len(starts) == 5:
                sender._stop.set()
            return True

    sender = ms.MetricsSender(Reporter(), _nopost, limit_fn=lambda: (cpu.ON, 50.0), clock=clock, wait_fn=clock.wait)
    t0 = clock.now
    sender.run()
    assert [round((at - t0) / P, 6) for at in starts] == [1, 2, 5, 6, 7]


def test_off_ticks_do_nothing():
    clock = FakeClock()
    posted = []

    class Reporter:
        def report_tick(self, send):
            raise AssertionError("no report while the gate is off")

    sender = ms.MetricsSender(Reporter(), lambda s, p: posted.append(s), limit_fn=lambda: (cpu.OFF, None),
                              clock=clock, wait_fn=clock.wait)
    for state in (cpu.OFF, cpu.UNKNOWN):
        sender.limit_fn = lambda s=state: (s, None)
        assert sender.tick() is None
    assert posted == [] and sender.on is False and sender.sent == sender.skipped == 0
    # A reporter without the tick seam (a loop test's stand-in) is never delegated to, even while ON.
    stand_in = ms.MetricsSender(SimpleNamespace(report_due=lambda: True), lambda s, p: posted.append(s),
                                limit_fn=lambda: (cpu.ON, 50.0), clock=clock)
    assert stand_in.tick() is None and stand_in.on is False and posted == []


def _toggling_world(state_at, alive_at=lambda t: True, *, loop_from=0.0):
    """A real Reporter shared by the loop (every 0.5 s from *loop_from*) and the sender (on its anchors), on one
    fake clock."""
    clock = FakeClock(0.0)
    sends: list[tuple[float, str]] = []
    state = [cpu.ON]
    caller = ["sender"]  # which path is reporting (the loop's takeover while ON uses the sender's transport)
    reporter = mm.Reporter(lambda s: sends.append((clock.now, "loop")), collect_fn=lambda: {"cpus": 2},
                           clock=clock, cpu_limit_fn=_limit(state), cpu_take_fn=lambda _l: dict(SAMPLE))
    sender = ms.MetricsSender(reporter, lambda s, p: sends.append((clock.now, caller[0])), limit_fn=_limit(state),
                              clock=clock, sched_fn=lambda: None)
    sender.alive = lambda: alive_at(clock.now)
    next_tick = P
    while clock.now < 200.0:
        state[0] = state_at(clock.now)
        if clock.now >= next_tick and alive_at(clock.now):
            sender.tick()
            sender.tick_done_at = clock.now
            next_tick += P
        if clock.now >= loop_from:
            caller[0] = "loop"
            sender.loop_report()
            caller[0] = "sender"
        clock.now = round(clock.now + 0.5, 6)
    return sends


def _assert_spaced_and_continuous(sends):
    times = [t for t, _ in sends]
    assert times == sorted(times)
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert min(gaps) >= P / 2, sends  # never two sends closer than half a period
    assert max(gaps) <= 2 * P + 0.5, sends  # and no silent stop at a hand-over


def test_on_off_on_never_double_sends_and_never_goes_silent():
    sends = _toggling_world(lambda t: cpu.OFF if 63.0 <= t < 121.5 else cpu.ON)
    _assert_spaced_and_continuous(sends)
    assert {who for t, who in sends if P < t < 63.0} == {"sender"}  # delegated while ON
    assert {who for t, who in sends if 75.0 <= t < 121.5} == {"loop"}  # the loop reports while OFF
    assert {who for t, who in sends if t >= 135.0} == {"sender"}


def test_sender_death_hands_reporting_back_to_the_loop_without_a_double_send():
    sends = _toggling_world(lambda t: cpu.ON, alive_at=lambda t: t < 87.3)
    _assert_spaced_and_continuous(sends)
    assert {who for t, who in sends if t >= 100.0} == {"loop"}


def test_the_first_anchor_skips_when_the_loop_reported_less_than_half_a_period_before():
    # The loop's first report comes after its first round (here 7 s in); the sender's first anchor is at 10 s.
    sends = _toggling_world(lambda t: cpu.ON, loop_from=7.0)
    _assert_spaced_and_continuous(sends)
    assert sends[:2] == [(7.0, "loop"), (20.0, "sender")]


def test_a_stalled_sender_hands_reporting_back_to_the_loop_with_a_warning(caplog):
    clock = FakeClock(0.0)
    sends = []
    reporter = mm.Reporter(lambda s: sends.append(("hub", clock.now)), collect_fn=lambda: {"cpus": 2}, clock=clock,
                           cpu_limit_fn=lambda: (cpu.ON, 50.0))
    # While ON the loop's takeover goes through the sender's bounded transport, never the kept-alive Hub.
    sender = ms.MetricsSender(reporter, lambda s, p: sends.append(clock.now), limit_fn=lambda: (cpu.ON, 50.0),
                              clock=clock, sched_fn=lambda: None)
    sender.alive = lambda: True
    sender.on = True  # mid-tick: ON is known, the tick's end not stamped yet
    assert sender.loop_report() is True and sends == [0.0]
    sends.clear()
    sender.on, sender.tick_done_at = True, 10.0
    clock.now = 10.0 + ms.STALL_PERIODS * P
    assert sender.loop_report() is False and sends == []
    clock.now += 0.5
    with caplog.at_level(logging.WARNING, logger=ms.__name__):
        assert sender.loop_report() is True and sends == [clock.now]
        clock.now += 1.0
        sender.loop_report()
    assert len([r for r in caplog.records if "metrics_sender stalled" in r.getMessage()]) == 1


def test_the_delegation_check_reads_no_config_and_never_takes_the_reporter_lock(monkeypatch):
    """S-2: the loop's per-iteration check is in-memory only."""
    def no_config(*_a, **_k):
        raise AssertionError("the loop's delegation check read the root config")
    monkeypatch.setattr(cpu, "root_limit", no_config)
    clock = FakeClock(0.0)
    reporter = mm.Reporter(lambda s: None, collect_fn=lambda: {"cpus": 2}, clock=clock)
    sender = ms.MetricsSender(reporter, _nopost, limit_fn=no_config, clock=clock)
    sender.alive = lambda: True
    sender.on, sender.tick_done_at = True, 0.0
    assert reporter._lock.acquire(blocking=False)  # a send in flight on the sender thread
    try:
        t0 = time.monotonic()
        for _ in range(1000):
            assert sender.loop_report() is False
        assert time.monotonic() - t0 < 1.0
    finally:
        reporter._lock.release()


def test_the_loop_and_the_sender_never_report_at_once():
    clock = FakeClock(0.0)
    reporter = mm.Reporter(lambda s: None, collect_fn=lambda: {"cpus": 2}, clock=clock,
                           cpu_limit_fn=lambda: (cpu.OFF, None))
    assert reporter._lock.acquire(blocking=False)  # a report is in flight on the other thread
    try:
        assert reporter.report_tick(lambda s: None) is None
        assert reporter.report_due() is False
    finally:
        reporter._lock.release()
    assert reporter.report_tick(lambda s: None) is True


def test_a_board_backoff_holds_the_sender_too():
    clock = FakeClock(0.0)

    def unsupported(_sample):
        raise client.HubError(404, "no route")
    reporter = mm.Reporter(unsupported, collect_fn=lambda: {"cpus": 2}, clock=clock,
                           cpu_limit_fn=lambda: (cpu.OFF, None))
    assert reporter.report_due() is False
    clock.now = mm.UNSUPPORTED_RETRY_S - P - 1
    assert reporter.report_tick(lambda s: None) is None
    clock.now = mm.UNSUPPORTED_RETRY_S - P
    assert reporter.report_tick(lambda s: None) is True


def _start(sender):
    sender.linux = True
    sender.start()
    return sender


@pytest.mark.parametrize("where", ["collect", "send", "tick"])
def test_a_raising_collect_send_or_tick_leaves_the_thread_alive(monkeypatch, where):
    monkeypatch.setattr(mm, "REPORT_EVERY_S", 0.02)

    def boom(*_a):
        raise RuntimeError(where)
    reporter = mm.Reporter(None, collect_fn=boom if where == "collect" else (lambda: {"cpus": 2}),
                           cpu_limit_fn=lambda: (cpu.ON, 50.0))
    if where == "tick":
        reporter.report_tick = boom
    sender = _start(ms.MetricsSender(reporter, boom if where == "send" else _nopost, limit_fn=lambda: (cpu.ON, 50.0)))
    try:
        wait_until(lambda: sender.ticks >= 5, timeout=10, what="five sender ticks")
        assert sender.alive()
        assert sender.failed >= 4 and sender.sent == 0
    finally:
        sender.close()
    assert not sender.alive()


def test_close_returns_within_3_s_with_a_send_in_flight(monkeypatch):
    monkeypatch.setattr(mm, "REPORT_EVERY_S", 0.02)
    release, entered = threading.Event(), threading.Event()

    def stuck(_sample, _probe):
        entered.set()
        release.wait(30)
    reporter = mm.Reporter(None, collect_fn=lambda: {"cpus": 2}, cpu_limit_fn=lambda: (cpu.ON, 50.0))
    sender = _start(ms.MetricsSender(reporter, stuck, limit_fn=lambda: (cpu.ON, 50.0)))
    try:
        assert entered.wait(10)
        t0 = time.monotonic()
        sender.close()
        assert time.monotonic() - t0 < 3.0
    finally:
        release.set()
    wait_until(lambda: not sender.alive(), timeout=10, what="the released sender to exit")


def test_the_thread_name_fits_the_kernel_comm_limit():
    assert len(ms.THREAD_NAME.encode()) <= 15
    sender = _start(ms.MetricsSender(SimpleNamespace(), _nopost, limit_fn=lambda: (cpu.OFF, None)))
    try:
        assert sender._thread.name == ms.THREAD_NAME
        if sys.platform.startswith("linux"):
            comm = Path(f"/proc/self/task/{sender._thread.native_id}/comm").read_text().strip()
            assert comm == ms.THREAD_NAME
    finally:
        sender.close()


def test_counts_are_logged_at_close(monkeypatch, caplog):
    monkeypatch.setattr(mm, "REPORT_EVERY_S", 0.02)
    reporter = mm.Reporter(None, collect_fn=lambda: {"cpus": 2}, cpu_limit_fn=lambda: (cpu.OFF, None))
    sender = _start(ms.MetricsSender(reporter, _nopost, limit_fn=lambda: (cpu.OFF, None)))
    wait_until(lambda: sender.ticks >= 2, timeout=10, what="two sender ticks")
    with caplog.at_level("INFO", logger=ms.__name__):
        sender.close()
    assert any(r.getMessage().startswith("metrics_sender ticks=") and "sent=0" in r.getMessage()
               for r in caplog.records)


def test_counts_are_logged_at_most_every_60_s(caplog):
    clock = FakeClock()
    sender = None
    n = [0]

    class Reporter:
        def report_tick(self, send):
            n[0] += 1
            if n[0] == 30:  # 300 s of ticks
                sender._stop.set()
            return True

    sender = ms.MetricsSender(Reporter(), _nopost, limit_fn=lambda: (cpu.ON, 50.0), clock=clock, wait_fn=clock.wait)
    with caplog.at_level("INFO", logger=ms.__name__):
        sender.run()
    lines = [r for r in caplog.records if r.getMessage().startswith("metrics_sender ticks=")]
    assert 3 <= len(lines) <= 5


def _fields(message: str) -> dict:
    return dict(part.split("=", 1) for part in message.split() if "=" in part)


def test_each_attempted_send_logs_timing_outcome_written_and_schedstat(caplog):
    """S-4: one INFO line per attempted send, the 400 compatibility retry included."""
    scheds = iter([(100, 50), (160, 80), (200, 90), (230, 95)])
    calls = []

    def post(sample, probe):
        calls.append(dict(sample))
        probe["written"] = True
        probe["worker"] = (7, 3)
        if len(calls) == 1:
            raise client.HubError(400, "unknown field(s): drain_active")
        return {}
    clock = FakeClock(0.0)
    reporter = mm.Reporter(None, collect_fn=lambda: {"cpus": 2, "drain_active": 0}, clock=clock,
                           cpu_limit_fn=lambda: (cpu.ON, 50.0), cpu_take_fn=lambda _l: dict(SAMPLE))
    sender = ms.MetricsSender(reporter, post, limit_fn=lambda: (cpu.ON, 50.0), clock=clock,
                              sched_fn=lambda: next(scheds))
    with caplog.at_level("INFO", logger=ms.__name__):
        assert sender.tick() is True
    lines = [_fields(r.getMessage()) for r in caplog.records if r.getMessage().startswith("metrics_sender send ")]
    assert len(lines) == 2 == len(calls)
    first, retry = lines
    assert int(first["t1"]) >= int(first["t0"]) > 0
    assert (first["end_ms"], first["win_ms"], first["out"], first["written"]) == ("1", "1000", "http400", "True")
    assert (first["run_ns"], first["wait_ns"], first["worker_run_ns"], first["worker_wait_ns"]) == ("60", "30", "7", "3")
    assert retry["out"] == "ok" and retry["end_ms"] == "None"  # the retry never resends the CPU triple
    assert (retry["run_ns"], retry["wait_ns"]) == ("30", "5")


class _SlowBoard(http.server.BaseHTTPRequestHandler):
    release = threading.Event()  # set at teardown: the Board answers only after the 2 s budget has cut the send
    seen: list = []

    def log_message(self, *_a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        _SlowBoard.seen.append(self.path)
        _SlowBoard.release.wait(30)
        try:
            self.send_response(200)
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
        except OSError:
            pass


@pytest.mark.platforms("linux")  # schedstat is Linux-only
def test_a_sample_without_a_cpu_triple_is_bounded_to_2_s_and_probed(caplog):
    """S-3: the sender never uses the 30 s kept-alive path, even for a report without a CPU triple."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowBoard)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ident = client.Identity(url=f"http://127.0.0.1:{server.server_address[1]}", key="k" * 16, gateway="gw")
        reporter = mm.Reporter(None, collect_fn=lambda: {"cpus": 2}, cpu_limit_fn=lambda: (cpu.ON, 50.0),
                               cpu_take_fn=lambda _l: None)  # no triple: report_metrics would use HTTP_TIMEOUT_S
        sender = ms.MetricsSender(reporter, ms.board_post(ident), limit_fn=lambda: (cpu.ON, 50.0))
        t0 = time.monotonic()
        with caplog.at_level("INFO", logger=ms.__name__):
            assert sender.tick() is False
        assert time.monotonic() - t0 < client.CPU_REPORT_BUDGET_S + 1.0
        line = next(_fields(r.getMessage()) for r in caplog.records
                    if r.getMessage().startswith("metrics_sender send "))
        assert line["out"] == "timeout" and line["written"] == "True" and line["end_ms"] == "None"
        assert int(line["worker_run_ns"]) > 0 and int(line["run_ns"]) >= 0
        assert _SlowBoard.seen == [ms.METRICS_PATH]
    finally:
        _SlowBoard.release.set()
        server.shutdown()


def test_non_linux_starts_no_thread_and_the_loop_reports_as_before():
    calls = []
    reporter = SimpleNamespace(report_due=lambda: calls.append(1) or True)
    sender = ms.MetricsSender(reporter, _nopost, limit_fn=lambda: (cpu.ON, 50.0), linux=False)
    assert sender.start() is None and not sender.alive()
    assert sender.loop_report() is True and calls == [1]
    sender.close()


def test_delegated_loop_report_is_a_component_free_not_sent_record(tmp_path, monkeypatch):
    h = timing_tests.Harness(tmp_path)
    h.arm()
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *a, **k: tmp_path)
    from hermes_cli import kanban_sync_timing as timing
    monkeypatch.setattr(timing, "_DIAG", h.diag)  # the Reporter's spans go through the module's diagnostics
    sent = []
    clock = lambda: h.clock.ns / 1e9
    reporter = mm.Reporter(sent.append, collect_fn=lambda: {"cpus": 4}, clock=clock,
                           cpu_limit_fn=lambda: (cpu.ON, 50.0), cpu_take_fn=lambda _l: None)
    sender = ms.MetricsSender(reporter, _nopost, limit_fn=lambda: (cpu.ON, 50.0), clock=clock)
    sender.alive = lambda: True
    sender.on, sender.tick_done_at = True, clock()
    assert h.diag.report(sender.loop_report) is False
    assert sent == []
    sender.on = False  # not delegated: the loop's full report, as before
    assert h.diag.report(sender.loop_report) is True
    assert sent == [{"cpus": 4}]
    h.until[0] = None
    h.diag.boundary()  # stop the capture: flushes its records
    reps = [r for r in h.records() if r["ev"] == "rep"]
    assert len(reps) == 2
    assert reps[0]["res"] == "not_sent" and not [k for k in reps[0] if k.endswith(("_t0", "_t1"))]
    assert reps[1]["res"] == "sent" and {"collect_t0", "limit_t0", "send_t0"} <= set(reps[1])


@pytest.mark.platforms("linux")  # the sender runs on Linux hosts only
def test_a_long_blocked_round_still_yields_reports_from_the_sender(tmp_path, monkeypatch):
    """A round blocked for 30 periods (the hub's 30 s round at a 0.1 s scale) still yields >= 3 reports."""
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *a, **k: tmp_path)
    monkeypatch.setattr(cpu, "root_limit", lambda *a, **k: (cpu.ON, 100.0))
    monkeypatch.setattr(mm, "REPORT_EVERY_S", 0.1)
    from hermes_cli import machine_cpu_sampler as cs

    class Sampler:
        def take(self, _limit):
            return {**SAMPLE, "cpu_busy_sample_end_ms": int(time.time() * 1000)}

        def wait_fresh(self, _timeout):
            return True  # a sample is always ready

        def start(self):
            return None

        def close(self):
            return None
    monkeypatch.setattr(cs, "AdvisorySampler", Sampler)
    sends, rounds, loop_hub_sends = [], [], []

    def bounded(ident, method, path, body, budget_s, probe=None):
        assert (method, path, budget_s) == ("POST", ms.METRICS_PATH, client.CPU_REPORT_BUDGET_S)
        sends.append((time.monotonic(), threading.current_thread().name, dict(body)))
        return {}

    def blocked_round(hub, *, boards):
        t0 = time.monotonic()
        wait_until(lambda: len([s for s in sends if s[0] >= t0]) >= 3, timeout=3.0 + 30 * mm.REPORT_EVERY_S,
                   what="three reports while the round is blocked")
        rounds.append((t0, time.monotonic()))
        return []

    monkeypatch.setattr(client, "load_identity", lambda: SimpleNamespace(gateway="test", url="https://example.invalid"))
    monkeypatch.setattr(client, "_schema_ahead_boards", lambda b: [])
    monkeypatch.setattr(client, "board_paths", lambda: [("a", Path("a.db"))])
    monkeypatch.setattr(client, "Hub", lambda i: SimpleNamespace(report_metrics=loop_hub_sends.append))
    monkeypatch.setattr(client, "_bounded_call", bounded)
    monkeypatch.setattr(client, "HubLeaseClient", lambda h: None)
    monkeypatch.setattr(client.sync, "set_lease_client", lambda h: None)
    monkeypatch.setattr(client, "deferred_repairs_due", lambda *a: [])
    monkeypatch.setattr(client, "next_deferred_repair", lambda *a: None)
    monkeypatch.setattr(client, "outbox_pending", lambda *a: False)
    monkeypatch.setattr(client, "reconcile_links", lambda *a, **kw: None)
    monkeypatch.setattr(client, "sync_all_once", blocked_round)
    monkeypatch.setattr(client, "FeedWatcher", lambda *a, **kw: SimpleNamespace(run=lambda: None))
    monkeypatch.setattr(client, "wait_for_work", lambda *a: "period")
    state = SimpleNamespace(clear=lambda: None, record_down=lambda msg: None)

    client._run_forever(lambda: len(rounds) < 1, 30.0, state)
    assert not [t for t in threading.enumerate() if t.name == ms.THREAD_NAME]  # closed with the loop
    (t0, t1), = rounds
    during = [s for s in sends if t0 <= s[0] <= t1]
    assert len(during) >= 3
    assert all(name == ms.THREAD_NAME for _, name, _ in sends)
    assert loop_hub_sends == []  # the loop's own Hub never reported (it was delegated)
    assert all(set(SAMPLE) <= set(body) for *_x, body in during)  # each carries the CPU triple
