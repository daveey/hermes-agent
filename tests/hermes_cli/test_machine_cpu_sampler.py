"""The hub CPU advisory sampler: rolling snapshots, at-most-once fresh report hand-off, fail-closed lifetime."""
import itertools
import logging
import random
import threading
import time

import pytest

from hermes_cli import machine_cpu_metrics as cpu
from hermes_cli import machine_cpu_sampler as cs
from hermes_cli import machine_metrics as mm

WALL0 = 1_700_000_000.0  # fake wall clock = WALL0 + fake monotonic
BOARD_CPU_MAX_DELAY_MS = 2000  # hermes-hub worker/src/mailbox.ts CPU_MAX_DELAY_MS (deployed d9e950ade108)


def _stat(busy, idle):
    return f"cpu {busy} 0 0 {idle} 0 0 0 0\n"


def _sampler(now, gate=None, stats=None, recorded=None):
    """A sampler on a fake clock. *stats* is a list of /proc/stat texts consumed one per step; when it runs out each
    read is 25 % busy over the elapsed fake time."""
    gate = gate if gate is not None else [(cpu.ON, 50.0)]
    stats = stats if stats is not None else []
    recorded = recorded if recorded is not None else []

    def read():
        if stats:
            text = stats.pop(0)
            if isinstance(text, Exception):
                raise text
            return text
        ticks = int(now[0] * 400)
        return _stat(ticks // 4, ticks - ticks // 4)
    return cs.AdvisorySampler(limit_fn=lambda: gate[0], read_fn=read, record_fn=recorded.append,
                              clock=lambda: now[0], wall=lambda: WALL0 + now[0])


def _end_ms(t):
    return int((WALL0 + t) * 1000)


def test_rolling_snapshots_form_one_sample_per_step_from_previous_and_new():
    now, recorded = [100.0], []
    s = _sampler(now, stats=[_stat(0, 0), _stat(25, 75), _stat(75, 125)], recorded=recorded)
    assert s.run_once() is None  # the first snapshot only primes the window
    now[0] += 1.0
    first = s.run_once()
    assert first == {"cpu_busy_pct": 25.0, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": _end_ms(101.0)}
    now[0] += 1.2
    second = s.run_once()  # (previous, new): the window starts where the last one ended
    assert second == {"cpu_busy_pct": 50.0, "cpu_busy_window_ms": 1200, "cpu_busy_sample_end_ms": _end_ms(102.2)}
    assert s.records == 2 and s.drops == 0


def test_each_valid_sample_is_reported_at_most_once():
    now = [100.0]
    s = _sampler(now)
    s.run_once()
    now[0] += 1.0
    sample = s.run_once()
    assert s.take(50.0) == sample
    assert s.take(50.0) is None  # never replayed into a second report
    now[0] += 1.0
    newer = s.run_once()
    assert newer != sample and s.take(50.0) == newer


def test_take_refuses_another_limit_a_stale_end_or_a_future_end():
    now = [100.0]
    s = _sampler(now)
    s.run_once()
    now[0] += 1.0
    sample = s.run_once()
    assert s.take(60.0) is None  # the limit changed since the measurement
    now[0] += cs.REPORT_MAX_AGE_MS / 1000 + 0.001
    assert s.take(50.0) is None  # ends more than 1.5 s before the take: the Board would drop it
    now[0] = 101.0 + cs.REPORT_MAX_AGE_MS / 1000
    assert s.take(50.0) == sample  # the age bound is inclusive
    t2 = [100.0]
    s2 = _sampler(t2)
    s2.run_once()
    t2[0] = 101.0
    s2.run_once()
    s2.wall = lambda: WALL0 + 100.5  # the wall clock stepped back past the end stamp
    assert s2.take(50.0) is None


@pytest.mark.parametrize("window_s", [0.999, 2.001])
def test_a_window_out_of_bounds_drops_the_sample_and_restarts_from_the_new_snapshot(caplog, window_s):
    now, recorded = [100.0], []
    s = _sampler(now, recorded=recorded)
    s.run_once()
    now[0] += window_s
    with caplog.at_level(logging.INFO, logger=cs.__name__):
        assert s.run_once() is None
    assert recorded == [] and s.drops == 1 and s.consecutive_drops == 1 and s.take(50.0) is None
    assert any("cpu_sample_drops=1 (consecutive=1" in m for m in caplog.messages)
    assert cs.status()["consecutive_drops"] == 1
    now[0] += 1.0  # the next window starts at the dropped one's end, not at the earlier snapshot
    after = s.run_once()
    assert after is not None and after["cpu_busy_window_ms"] == 1000
    assert s.consecutive_drops == 0 and len(recorded) == 1 and cs.status()["consecutive_drops"] == 0


def test_an_unreadable_snapshot_is_counted_and_never_recorded(caplog):
    now, recorded = [100.0], []
    s = _sampler(now, stats=[_stat(0, 0), OSError("gone"), "garbage\n"], recorded=recorded)
    s.run_once()
    with caplog.at_level(logging.INFO, logger=cs.__name__):
        for _ in range(2):
            now[0] += 1.0
            assert s.run_once() is None
    assert recorded == [] and s.drops == 2 and s.consecutive_drops == 2 and s.take(50.0) is None
    assert any("cpu_sample_drops=2 (consecutive=2" in m for m in caplog.messages)
    now[0] += 1.0
    assert s.run_once() is None  # a fresh start after the unreadable ones: this snapshot primes the window
    now[0] += 1.0
    assert s.run_once() is not None and len(recorded) == 1


def test_off_linux_the_default_reader_is_a_drop():
    s = cs.AdvisorySampler(limit_fn=lambda: (cpu.ON, 50.0), record_fn=lambda _s: None, linux=False)
    assert s.run_once() is None and s.drops == 1


@pytest.mark.parametrize("gate", [(cpu.OFF, None), (cpu.UNKNOWN, None)])
def test_gate_not_on_reads_nothing_and_forgets_the_last_sample_and_snapshot(gate):
    now, reads = [100.0], []
    g = [(cpu.ON, 50.0)]
    s = _sampler(now, gate=g)
    s.run_once()
    now[0] += 1.0
    s.run_once()
    g[0] = gate
    real_read = s.read_fn
    assert real_read is not None
    s.read_fn = lambda: reads.append(1) or real_read()
    now[0] += 1.0
    assert s.run_once() is None and reads == [] and s.drops == 0
    assert s.take(50.0) is None
    g[0] = (cpu.ON, 50.0)
    now[0] += 1.0
    assert s.run_once() is None  # back on: a new first snapshot, never a window spanning the off period


def test_advisory_file_is_written_at_most_every_five_seconds_from_the_newest_valid_sample():
    now, recorded = [0.0], []
    s = _sampler(now, recorded=recorded)
    s.run_once()
    for _ in range(11):
        now[0] += 1.0
        s.run_once()
    assert [r["cpu_busy_sample_end_ms"] for r in recorded] == [_end_ms(1.0), _end_ms(6.0), _end_ms(11.0)]
    assert s.records == 11  # every step still hands its sample to the report
    # A dropped step at the 5 s mark: the next valid one is written at once.
    now[0] += 4.0
    s.run_once()  # 15.0: a 4 s window, dropped
    now[0] += 1.0
    s.run_once()  # 16.0: valid, 5 s after the last write
    assert recorded[-1]["cpu_busy_sample_end_ms"] == _end_ms(16.0)


def test_record_failure_still_hands_the_sample_to_the_report_and_retries_the_write():
    now, calls = [100.0], []
    s = _sampler(now)

    def fail(sample):
        calls.append(sample)
        raise OSError("read-only cache")
    s.record_fn = fail
    s.run_once()
    now[0] += 1.0
    sample = s.run_once()
    assert s.take(50.0) == sample
    now[0] += 1.0
    s.run_once()
    assert len(calls) == 2  # a failed write is not a write: the next step tries again


def test_default_record_writes_the_advisory_file(tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *a, **k: tmp_path)
    now = [100.0]
    s = _sampler(now)
    s.record_fn = None
    s.run_once()
    now[0] += 1.0
    assert mm.read_hub_cpu_sample(tmp_path) is None
    sample = s.run_once()
    assert mm.read_hub_cpu_sample(tmp_path) == sample


def test_thread_keeps_its_own_cadence_and_stops_on_close(monkeypatch):
    monkeypatch.setattr(cpu, "WINDOW_S", 0.01)
    rounds = threading.Semaphore(0)
    s = cs.AdvisorySampler(limit_fn=lambda: (cpu.ON, 50.0), read_fn=lambda: rounds.release() or _stat(1, 1),
                           record_fn=lambda _s: None)
    thread = s.start()
    assert thread.daemon and thread.name == "hub-cpu-advisory-sampler"
    for _ in range(3):  # several steps without any caller driving it
        assert rounds.acquire(timeout=5)
    s.close()
    assert not thread.is_alive()


def test_close_wakes_the_wait_at_once():
    idle = cs.AdvisorySampler(limit_fn=lambda: (cpu.OFF, None))  # mid-wait on the default 1 s window
    t2 = idle.start()
    started = time.monotonic()
    idle.close()
    assert not t2.is_alive() and time.monotonic() - started < cpu.WINDOW_S


def test_a_wait_that_wakes_early_never_shortens_the_window():
    now, waits, windows = [100.0], [], []
    s = _sampler(now)

    def early_wait(timeout):  # wakes 40 % early every time
        waits.append(timeout)
        now[0] += timeout * 0.6
        if len(waits) > 40:
            s._stop.set()
    s._wait = early_wait
    real = s.run_once

    def step():
        out = real()
        if out:
            windows.append(out["cpu_busy_window_ms"])
        return out
    s.run_once = step
    s.run()
    assert windows and all(w >= cpu.MIN_WINDOW_MS for w in windows) and s.drops == 0, (windows, s.drops)


def test_a_failing_step_does_not_end_the_thread(monkeypatch):
    monkeypatch.setattr(cpu, "WINDOW_S", 0.01)
    rounds = threading.Semaphore(0)

    def limit():
        rounds.release()
        raise RuntimeError("controlled")
    s = cs.AdvisorySampler(limit_fn=limit)
    s.start()
    assert rounds.acquire(timeout=5) and rounds.acquire(timeout=5)
    s.close()


def test_advisory_write_cadence_is_five_windows():
    assert cs.record_every_s() == 5 * cpu.WINDOW_S == 5.0


# --- The real sampler thread + the real Reporter on one fake clock (sync rounds of 3-20 s) -------------------------

class _LockstepClock:
    """Two actors share one fake monotonic clock: the sampler's real ``run`` thread and the test's sync loop. Exactly
    one runs at a time; the one that waits for the earliest time runs next, so the interleaving is deterministic."""

    def __init__(self):
        self.now = 0.0
        self.cond = threading.Condition()
        self.waiting: dict = {}
        self.turn = "loop"
        self.stopped = False

    def clock(self):
        return self.now

    def wall(self):
        return WALL0 + self.now

    def sleep_until(self, actor, at):
        with self.cond:
            self.waiting[actor] = max(at, self.now)
            if self.turn == actor:
                self.turn = None
            if self.turn is None and len(self.waiting) == 2:
                nxt = min(self.waiting, key=lambda a: (self.waiting[a], a))
                self.now = self.waiting.pop(nxt)
                self.turn = nxt
                self.cond.notify_all()
            ok = self.cond.wait_for(lambda: self.stopped or self.turn == actor, timeout=30)
            assert ok, "lockstep clock stalled"
            self.waiting.pop(actor, None)

    def stop(self):
        with self.cond:
            self.stopped = True
            self.cond.notify_all()


def _simulate(seed, *, rounds=200, stall_p=0.0):
    """Run the real sampler thread and a sync loop of 3-20 s rounds that calls the real ``Reporter.report_due`` at each
    round end. Returns (sent [(send start wall ms, metrics)], due count, advisory-file write times)."""
    rng = random.Random(seed)
    lock = _LockstepClock()
    records = []

    def read():  # 4 CPUs at 100 Hz, ~30 % busy: counters grow with the fake clock
        ticks = int(lock.now * 400)
        return _stat(int(ticks * 0.3), ticks - int(ticks * 0.3))

    def sampler_wait(timeout):  # scheduling jitter up to 30 ms; with stall_p, a starved thread misses 2-4 s
        late = rng.uniform(2.0, 4.0) if rng.random() < stall_p else rng.uniform(0.0, 0.03)
        lock.sleep_until("sampler", lock.now + timeout + late)

    sampler = cs.AdvisorySampler(limit_fn=lambda: (cpu.ON, 50.0), read_fn=read,
                                 record_fn=lambda _s: records.append(lock.now), clock=lock.clock, wall=lock.wall,
                                 wait_fn=sampler_wait)
    sent = []
    reporter = mm.Reporter(lambda m: sent.append((int(lock.wall() * 1000), dict(m))), collect_fn=lambda: {"cpus": 4},
                           clock=lock.clock, cpu_limit_fn=lambda: (cpu.ON, 50.0), cpu_take_fn=sampler.take)
    due = 0
    lock.turn = "sampler"  # the sampler's first step runs before the first round ends
    sampler.start()
    try:
        for _ in range(rounds):
            lock.sleep_until("loop", lock.now + rng.uniform(3.0, 20.0))  # one sync round
            due += reporter.report_due()
    finally:
        lock.stop()
        sampler.close()
    assert sampler._thread is not None and not sampler._thread.is_alive()
    return sent, due, records


@pytest.mark.parametrize("seed", range(5))
def test_every_reported_triple_is_fresh_and_nearly_every_due_report_carries_one(seed):
    sent, due, records = _simulate(seed)
    assert due >= 100 and len(sent) == due
    cpu_reports = [(at, m) for at, m in sent if all(k in m for k in cpu.FIELDS)]
    ages = [at - m["cpu_busy_sample_end_ms"] for at, m in cpu_reports]
    assert all(0 <= a <= cs.REPORT_MAX_AGE_MS for a in ages), max(ages)
    assert len(cpu_reports) * 10 >= 9 * due, (len(cpu_reports), due)
    assert len({m["cpu_busy_sample_end_ms"] for _at, m in cpu_reports}) == len(cpu_reports)  # never reported twice
    gaps = [b - a for a, b in itertools.pairwise(records)]
    assert records and max(gaps) < 15.0, max(gaps)  # 3-20 s rounds never stale the advisory past 15 s


@pytest.mark.parametrize("seed", range(5))
def test_the_board_window_accepts_every_triple_sent_even_when_the_sampler_stalls(seed):
    rng = random.Random(1000 + seed)
    for stall_p in (0.0, 0.2):
        sent, _due, _records = _simulate(seed, stall_p=stall_p)
        triples = [(at, m) for at, m in sent if all(k in m for k in cpu.FIELDS)]
        assert triples
        for send_start, m in triples:  # hermes-hub setOwnMetrics: at - CPU_MAX_DELAY_MS <= end <= at
            at = send_start + rng.randint(0, 400)
            assert at - BOARD_CPU_MAX_DELAY_MS <= m["cpu_busy_sample_end_ms"] <= at, (at, m)
