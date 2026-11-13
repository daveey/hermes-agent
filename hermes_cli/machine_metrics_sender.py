"""Hub metrics reports on their own anchored thread, independent of the sync round's length. [daveey]

While the installation-root CPU gate (``kanban.admission.cpu_busy_below_pct``) is on, the hub's machine-metrics
report carries the measured CPU triple the Board's hub-overflow admission reads. The sync loop sent it once per
iteration, after ``sync_all_once`` and ``reconcile_links``, so a long round held every report; the loop's wait
ignored the report's due time; and ``Reporter.report_due`` set ``next_at = now + REPORT_EVERY_S`` when it was
entered, so every late call pushed the next one later and a tick that woke earlier than the one before was dropped:
15-20 CPU-bearing reports per 300 s instead of 30, with 18-39 s stalls (2026-11-12, hermes-hub).

:class:`MetricsSender` is a daemon thread (``metrics-sender``, unique within the 15-byte comm limit) the sync loop
starts beside the CPU advisory sampler and closes (bounded join) in the same ``finally``:

- Tick ``k`` is due at ``t0 + k * REPORT_EVERY_S`` on the monotonic clock. A tick that overruns one or more anchors
  skips them (no burst): the next tick is the next future anchor, so cadence never drifts with send time. When a late
  send came less than half a period before that anchor, the tick waits until it is spaced instead of skipping.
- With no fresh CPU sample (the sampler restarts its window after a process-wide stall) a tick waits at most
  ``machine_metrics.CPU_WAIT_S`` for the next one; still none, and a report went out within ``KEEPALIVE_S``, the
  CPU-less report is withheld (it would erase the Board row's triple) and logged as ``metrics_sender withheld``.
- Each tick reads the root limit. ON: it runs the Reporter's unchanged body (collect, limit, take, send; a CPU sample
  is never replayed) through :meth:`Reporter.report_tick`, whose due rule is the anchor itself plus one guard: no
  report starts less than ``REPORT_EVERY_S / 2`` after the latest report start on either path (and the Board's
  404/405 backoff holds). Not ON: it does nothing that tick.
- Every send of this thread (CPU-bearing or not, the 400 compatibility retries included) goes through
  ``kanban_sync_client._bounded_call``: a private Hub(ident) and connection per send, a hard 2 s wall budget, never
  resent. The loop's kept-alive ``hub`` is never touched. On eligible 400 responses, compatibility retries drop the
  model map, then throughput counters, then drain/admission fields; CPU busy fields are stripped on the first retry.
  This permits four sequential sends, each bounded to 2 s, for at most 4 x 2 s = 8 s of send time; ordinary reports make one.
- The loop still calls ``sync_timing.report(sender.loop_report)`` every iteration. While this thread is alive, saw
  the root limit ON at its latest tick and finished that tick within :data:`STALL_PERIODS` periods, ``loop_report``
  returns False without collecting or sending (a component-free ``not_sent`` timing record). That check reads
  in-memory state only: no config read, no Reporter lock. Otherwise (gate off, non-Linux, thread dead or stalled) it
  is exactly ``Reporter.report_due``, so worker and gate-off hosts are unchanged; with the gate ON (thread dead or
  stalled) that report goes through this thread's bounded transport, never the kept-alive Hub. A stalled tick that
  still holds the Reporter blocks the loop's report too: the stall WARNING says whether a tick is in flight.
- No exception ends the thread. INFO lines: one per attempted send (``metrics_sender send ...``: monotonic t0/t1 ns,
  the triple's end_ms/win_ms, outcome, whether request bytes were written, and schedstat run/wait ns of this thread
  over the send and of the send's bounded-call worker), ``metrics_sender withheld ...`` per withheld report, and
  ``metrics_sender ticks=N sent=N failed=N skipped=N withheld=N`` at
  most every :data:`LOG_EVERY_S` and at close. A WARNING when the loop takes reporting back from a stalled sender.

Thread safety of what a tick reads (checked in source): ``collect()`` opens its own read-only SQLite connection per
board per call (``active_card_runs``, ``machine_throughput_metrics._read``) and closes it; the throughput cache is a
module global rebound atomically to an immutable tuple (a race only recomputes it); drain holds and ``/proc`` are
plain file reads; ``AdvisorySampler.take`` holds the sampler's lock. ``kanban_sync_timing`` spans no-op off the loop
thread. The thread runs in a copy of the loop thread's contextvars, so path resolution matches the loop's.
"""
from __future__ import annotations

import contextvars
import logging
import math
from threading import Event, Thread, current_thread  # bound now: loop tests stub threading.Thread
import sys
import time
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)

THREAD_NAME = "metrics-sender"  # <= 15 bytes: the kernel comm name stays unique
LOG_EVERY_S = 60.0
# The loop takes reporting back when the sender has not finished a tick for this many periods.
STALL_PERIODS = 3
CLOSE_JOIN_S = 2.5
METRICS_PATH = "/v2/machines/me/metrics"


def _period() -> float:
    from hermes_cli import machine_metrics as mm
    return float(mm.REPORT_EVERY_S)  # read per use: tests scale the period down


def board_post(ident: Any) -> Callable[[dict, dict], Any]:
    """The sender's transport: one bounded POST per call (private connection, CPU_REPORT_BUDGET_S, never resent)."""
    def post(sample: dict, probe: dict) -> Any:
        from hermes_cli import kanban_sync_client as ksc
        return ksc._bounded_call(ident, "POST", METRICS_PATH, sample, ksc.CPU_REPORT_BUDGET_S, probe=probe)
    return post


def _outcome(exc: Optional[BaseException]) -> str:
    if exc is None:
        return "ok"
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return f"http{status}"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "os" if isinstance(exc, OSError) else type(exc).__name__


def _pair(value: Any) -> tuple[Any, Any]:
    return value if isinstance(value, tuple) else (None, None)


class MetricsSender:
    def __init__(self, reporter: Any, post: Callable[[dict, dict], Any], *,
                 limit_fn: Optional[Callable[[], tuple]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 wait_fn: Optional[Callable[[float], Any]] = None, linux: Optional[bool] = None,
                 sched_fn: Optional[Callable[[], Optional[tuple[int, int]]]] = None) -> None:
        self.reporter, self.post = reporter, post
        self.limit_fn, self.clock = limit_fn, clock
        self.linux = sys.platform.startswith("linux") if linux is None else linux
        self.sched_fn = sched_fn
        self.on = False  # the root limit was ON at this thread's latest tick
        self.ticks = self.sent = self.failed = self.skipped = self.withheld = 0
        self.tick_done_at: Optional[float] = None
        self.tick_started_at: Optional[float] = None
        self._stall_warned = False
        self._stop = Event()
        self._wait = wait_fn or self._stop.wait
        self._thread: Optional[Thread] = None
        self._logged_at: Optional[float] = None

    def _limit(self) -> tuple:
        from hermes_cli import machine_cpu_metrics as cpu
        return (self.limit_fn or cpu.root_limit)()

    def _sched(self) -> Optional[tuple[int, int]]:
        if self.sched_fn is not None:
            return self.sched_fn()
        from hermes_cli.kanban_sync_bounded_probe import schedstat
        return schedstat()

    def send(self, sample: dict) -> Any:
        """One attempted send, logged at INFO with its timing, outcome and scheduler cost."""
        probe: dict = {}
        before = self._sched()
        t0 = time.monotonic_ns()
        exc: Optional[BaseException] = None
        try:
            return self.post(sample, probe)
        except BaseException as caught:
            exc = caught
            raise
        finally:
            t1 = time.monotonic_ns()
            after = self._sched()
            run_ns = wait_ns = None
            if before is not None and after is not None:
                run_ns, wait_ns = after[0] - before[0], after[1] - before[1]
            worker_run, worker_wait = _pair(probe.get("worker"))
            _log.info("metrics_sender send t0=%d t1=%d end_ms=%s win_ms=%s out=%s written=%s run_ns=%s wait_ns=%s "
                      "worker_run_ns=%s worker_wait_ns=%s", t0, t1, sample.get("cpu_busy_sample_end_ms"),
                      sample.get("cpu_busy_window_ms"), _outcome(exc), probe.get("written"), run_ns, wait_ns,
                      worker_run, worker_wait)

    def tick(self) -> Optional[bool]:
        """One anchored tick: True sent, False the report failed, None nothing sent (off or skipped). One
        ``machine_metrics.tick`` pass (loop_accounting); a tick that sends nothing is a skip."""
        from hermes_cli import loop_accounting
        with loop_accounting.pass_("machine_metrics.tick") as acct:
            sent = self._tick()
            if sent is None:
                acct.skip()
            return sent

    def _tick(self) -> Optional[bool]:
        from hermes_cli import machine_cpu_metrics as cpu
        self.ticks += 1
        self.tick_started_at = self.clock()
        try:
            state = self._limit()[0]
        except Exception:  # health: allow BLE001 -- an unreadable limit is "not ON" for this tick
            state = cpu.UNKNOWN
        self.on = state == cpu.ON
        if not self.on:
            return None
        report_tick = getattr(self.reporter, "report_tick", None)
        if report_tick is None:  # a reporter without the tick seam (a stand-in): never delegate to it
            self.on = False
            return None
        withheld_before = getattr(self.reporter, "suppressed", 0)
        result = report_tick(self.send)
        if result is True:
            self.sent += 1
        elif result is False:
            self.failed += 1
        elif getattr(self.reporter, "suppressed", 0) != withheld_before:
            # No fresh CPU sample even after the bounded wait: a CPU-less send would erase the row's triple.
            self.withheld += 1
            _log.info("metrics_sender withheld t=%d reason=no_cpu_sample", time.monotonic_ns())
        else:
            self.skipped += 1
        return result

    def _due(self, anchor: float) -> float:
        """The anchor, or later when a late send came less than half a period before it: the next report waits until
        it is spaced instead of being skipped (hermes-hub 2026-11-12 13:42:48 / 13:43:28: a late tick right after a
        stall made the next anchor's tick skip, ``skipped=1``/``2``, losing a CPU-bearing report)."""
        started = getattr(self.reporter, "started_at", None)
        if isinstance(started, (int, float)):
            return max(anchor, started + _period() / 2)
        return anchor

    def run(self) -> None:
        t0 = self.clock()
        k = 1
        while not self._stop.is_set():
            due = self._due(t0 + k * _period())
            while not self._stop.is_set() and self.clock() < due:
                self._wait(due - self.clock())
            if self._stop.is_set():
                break
            try:
                self.tick()
            except Exception as exc:  # health: allow BLE001 -- the sender must keep ticking
                self.failed += 1
                _log.debug("machine metrics sender tick failed (%s: %s)", type(exc).__name__, exc)
            now = self.clock()
            self.tick_done_at = now
            self._stall_warned = False
            # The next future anchor: an overrun skips the anchors it covered, never bursts to catch up.
            k = max(k + 1, math.floor((now - t0) / _period()) + 1)
            if self._logged_at is None:
                self._logged_at = now
            elif now - self._logged_at >= LOG_EVERY_S:
                self._logged_at = now
                self._log_counts()

    def _log_counts(self) -> None:
        _log.info("metrics_sender ticks=%d sent=%d failed=%d skipped=%d withheld=%d", self.ticks, self.sent,
                  self.failed, self.skipped, self.withheld)

    def alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def delegating(self) -> bool:
        """True while this thread owns hub reporting: alive, the root limit ON at its latest tick, and that tick
        finished within STALL_PERIODS periods. In-memory state only: no config read, no Reporter lock."""
        if not (self.alive() and self.on and self.tick_done_at is not None):
            return False  # tick_done_at: ``on`` is set inside a tick, before the tick's end is stamped
        if self.clock() - self.tick_done_at <= STALL_PERIODS * _period():
            return True
        if not self._stall_warned:
            self._stall_warned = True
            now, started = self.clock(), self.tick_started_at
            in_flight = (f"tick in flight for {now - started:.0f} s" if started is not None
                         and started > self.tick_done_at else "no tick in flight")
            # Not "the loop reports meanwhile": while the stalled tick holds the Reporter the loop cannot report.
            _log.warning("metrics_sender stalled: no tick finished for %.0f s (%s); the sync loop reports when the "
                         "Reporter is free", now - self.tick_done_at, in_flight)
        return False

    def loop_report(self) -> Any:
        """The sync loop's report callable (wrapped by ``sync_timing.report``)."""
        if self.delegating():
            return False
        if self.on:
            # Gate ON but the sender is stalled or dead: the loop reports through this thread's bounded transport
            # (2 s, private connection), never the kept-alive Hub's 30 s path while holding the Reporter lock.
            withheld_before = getattr(self.reporter, "suppressed", 0)
            result = self.reporter.report_due(self.send)
            if getattr(self.reporter, "suppressed", 0) != withheld_before:
                self.withheld += 1
                _log.info("metrics_sender withheld t=%d reason=no_cpu_sample path=loop", time.monotonic_ns())
            return result
        return self.reporter.report_due()

    def start(self) -> Optional[Thread]:
        if not self.linux:
            return None  # non-Linux hosts carry no CPU triple: the loop reports exactly as before
        ctx = contextvars.copy_context()  # the loop thread's profile scope, so collect() resolves the same paths
        self._thread = Thread(target=ctx.run, args=(self.run,), daemon=True, name=THREAD_NAME)
        self._thread.start()
        return self._thread

    def close(self, timeout: float = CLOSE_JOIN_S) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join(timeout)
        if thread is not None:
            self._log_counts()
